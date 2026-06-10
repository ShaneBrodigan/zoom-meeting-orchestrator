"""Read and write the per-session files in S3.

This is the one place that knows the S3 key layout (REFACTOR_DESIGN.md section 3)::

    s3://zoom-bot-dataset-s3/
      input_audio/librispeech_audio.pcm     # shared source
      sessions/{session_id}/
        spec.json                               # VM4 -> clients
        heartbeats/{ip}.json                     # each agent/bot -> VM4
        capture.pcap                            # VM4, post-call
        manifest.json                           # VM4, post-call

Both VM4 (the orchestrator) and the clients use this module, so the layout and the
JSON <-> schema conversion live in exactly one place. Callers speak in domain terms
("publish this spec", "read my spec", "record these heartbeats") and never touch raw
keys or boto3.

The boto3 client is injectable. On the VMs the default client authenticates through the
instance IAM role; tests pass a small in-memory fake so the read/write/parse logic can
be exercised without AWS. boto3 is imported lazily, only when a real client is built, so
this module imports fine on machines without boto3.
"""

from __future__ import annotations

import json
from typing import Any

from common.noise_config import NoiseConfig
from common.schema import HeartbeatEvent, Manifest, Spec

DEFAULT_BUCKET = "zoom-bot-dataset-s3"
DEFAULT_REGION = "eu-west-1"
AUDIO_SOURCE_KEY = "input_audio/librispeech_audio.pcm"
# Single source of truth for VM5 noise (decision 10): NOT per-session, lives outside
# sessions/. VM5 reads it to run noise; VM4 reads it to stamp the spec/manifest.
NOISE_CONFIG_KEY = "config/noise.json"


class SessionStore:
    """Typed access to one session's files in the dataset bucket."""

    def __init__(self, bucket: str = DEFAULT_BUCKET, *, client: Any = None,
                 region_name: str = DEFAULT_REGION) -> None:
        self.bucket = bucket
        self._client = client if client is not None else _make_boto3_client(region_name)

    # --- key layout (the single source of truth for where things live) ----- #

    @staticmethod
    def spec_key(session_id: str) -> str:
        return f"sessions/{session_id}/spec.json"

    @staticmethod
    def heartbeat_key(session_id: str, ip: str) -> str:
        return f"sessions/{session_id}/heartbeats/{ip}.json"

    @staticmethod
    def heartbeats_prefix(session_id: str) -> str:
        return f"sessions/{session_id}/heartbeats/"

    @staticmethod
    def capture_key(session_id: str) -> str:
        return f"sessions/{session_id}/capture.pcap"

    @staticmethod
    def manifest_key(session_id: str) -> str:
        return f"sessions/{session_id}/manifest.json"

    # --- orchestrator side (VM4) ------------------------------------------- #

    def publish_spec(self, spec: Spec) -> str:
        """Write spec.json. After this the clients can see (and join) the session."""
        key = self.spec_key(spec.session_id)
        self._put_json(key, spec.to_dict())
        return key

    def write_manifest(self, manifest: Manifest) -> str:
        """Write the post-call raw-facts manifest."""
        key = self.manifest_key(manifest.session_id)
        self._put_json(key, manifest.to_dict())
        return key

    def upload_capture(self, session_id: str, local_pcap_path: str) -> str:
        """Upload the captured pcap and return its key (for the manifest)."""
        key = self.capture_key(session_id)
        self._client.upload_file(local_pcap_path, self.bucket, key)
        return key

    def read_all_heartbeats(self, session_id: str) -> list[HeartbeatEvent]:
        """Read every client's heartbeat file and flatten into one time-sorted list."""
        events: list[HeartbeatEvent] = []
        for key in self._list_keys(self.heartbeats_prefix(session_id)):
            raw = self._get_json(key)
            events.extend(HeartbeatEvent.from_dict(e) for e in raw)
        events.sort(key=lambda e: e.ts)
        return events

    # --- client side (VM1/2/3/5) ------------------------------------------- #

    def read_spec(self, session_id: str) -> Spec:
        """Fetch and parse a session's spec. Raises if it does not exist."""
        return Spec.from_dict(self._get_json(self.spec_key(session_id)))

    def read_spec_with_anchor(self, session_id: str) -> tuple[Spec, float | None]:
        """Fetch a spec together with its publish time — the session's t=0 anchor.

        The anchor is the spec object's S3 ``LastModified``: the instant VM4 published
        it, which by construction is when the call's timeline starts (the orchestrator
        publishes the spec, then sleeps the call ``duration``). Because every client
        reads the *same* object they all share one anchor, so the turn windows (which
        are relative to t=0) line up across subnets without VM4 having to put an
        absolute timestamp into the frozen contract. Comparing it to a client's own
        clock relies on the chrony / AWS Time Sync alignment that is already a
        prerequisite. Returns ``(spec, anchor_epoch)``."""
        resp = self._client.get_object(Bucket=self.bucket, Key=self.spec_key(session_id))
        spec = Spec.from_dict(json.loads(_read_body(resp["Body"])))
        return spec, _to_epoch(resp.get("LastModified"))

    def read_heartbeats_for_ip(self, session_id: str, ip: str) -> list[HeartbeatEvent]:
        """Read just this client's own heartbeat file (empty list if not written yet).

        Lets a writer merge its new event with what is already there, so the agent
        (``launched``/``failed``) and the bot (``joined``/``left``) — both writing this
        one IP's file from different processes — don't clobber each other."""
        key = self.heartbeat_key(session_id, ip)
        if not self._object_exists(key):
            return []
        return [HeartbeatEvent.from_dict(e) for e in self._get_json(key)]

    def spec_exists(self, session_id: str) -> bool:
        return self._object_exists(self.spec_key(session_id))

    def list_session_ids(self) -> list[str]:
        """List the session ids that have a folder under sessions/."""
        ids: list[str] = []
        for prefix in self._list_common_prefixes("sessions/"):
            # prefix looks like "sessions/{id}/"
            parts = prefix.split("/")
            if len(parts) >= 2 and parts[1]:
                ids.append(parts[1])
        return sorted(ids)

    def write_heartbeats(self, session_id: str, ip: str,
                         events: list[HeartbeatEvent]) -> str:
        """Write this client's heartbeat file (the whole event list).

        Each client is the only writer of its own ``{ip}.json``, so overwriting with the
        full list is safe — there is no concurrent writer to race with."""
        key = self.heartbeat_key(session_id, ip)
        self._put_json(key, [e.to_dict() for e in events])
        return key

    def download_audio_source(self, local_path: str, *, key: str = AUDIO_SOURCE_KEY) -> None:
        """Fetch the shared LibriSpeech source file to a local path."""
        self._client.download_file(self.bucket, key, local_path)

    # --- shared infra config (read by both VM5 and VM4) -------------------- #

    def read_noise_config(self) -> NoiseConfig:
        """Read ``config/noise.json``, the single source of truth for VM5 noise.

        Not a per-session file: VM5 reads it to run the noise loop, VM4 reads it to
        stamp the matching ``noise`` block into each spec/manifest (decision 10)."""
        return NoiseConfig.from_dict(self._get_json(NOISE_CONFIG_KEY))

    # --- low-level helpers (the only spots that talk to the client) -------- #

    def _put_json(self, key: str, obj: Any) -> None:
        body = json.dumps(obj, indent=2).encode("utf-8")
        self._client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                ContentType="application/json")

    def _get_json(self, key: str) -> Any:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return json.loads(_read_body(resp["Body"]))

    def _list_keys(self, prefix: str) -> list[str]:
        resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return sorted(item["Key"] for item in resp.get("Contents", []))

    def _list_common_prefixes(self, prefix: str) -> list[str]:
        resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=prefix,
                                            Delimiter="/")
        return [cp["Prefix"] for cp in resp.get("CommonPrefixes", [])]

    def _object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            # boto3 raises ClientError (404) when the object is absent.
            return False


def _read_body(body: Any) -> bytes:
    """boto3 returns a streaming body with .read(); the fake returns bytes directly."""
    return body.read() if hasattr(body, "read") else body


def _to_epoch(value: Any) -> float | None:
    """Normalize an S3 ``LastModified`` to epoch seconds.

    Real boto3 hands back a timezone-aware ``datetime``; the in-memory test fake
    returns a plain float. ``None`` (a deficient response) passes through as ``None``."""
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return value.timestamp()
    return float(value)


def _make_boto3_client(region_name: str) -> Any:
    import boto3  # imported lazily so the module loads without boto3 present

    return boto3.client("s3", region_name=region_name)