"""Checkpoint tests for the S3 session store (common/s3.py).

Local, no AWS: a tiny in-memory fake stands in for the boto3 client, so the key layout
and the JSON <-> schema conversion are fully exercised here. The only thing left to
verify on a real VM is bucket connectivity / IAM, not this logic.

Run with:  pytest tests/test_s3.py
"""

import io
import json

import pytest

from common import schema
from common.s3 import SessionStore
from common.schema import (
    HeartbeatEvent,
    Manifest,
    Meeting,
    RosterEntry,
    Seeds,
    Spec,
    Timing,
    Turns,
    TurnWindow,
    Capture,
)


class FakeS3:
    """Minimal in-memory stand-in for boto3's s3 client."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(f"NoSuchKey: {Key}")
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(f"404: {Key}")
        return {}

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None):
        keys = [k for k in self.objects if k.startswith(Prefix)]
        if Delimiter is None:
            return {"Contents": [{"Key": k} for k in keys]}
        prefixes = set()
        for k in keys:
            rest = k[len(Prefix):]
            if Delimiter in rest:
                prefixes.add(Prefix + rest.split(Delimiter)[0] + Delimiter)
        return {"CommonPrefixes": [{"Prefix": p} for p in sorted(prefixes)]}

    def upload_file(self, local_path, Bucket, Key):
        with open(local_path, "rb") as f:
            self.objects[Key] = f.read()

    def download_file(self, Bucket, Key, local_path):
        with open(local_path, "wb") as f:
            f.write(self.objects[Key])


def make_store() -> tuple[SessionStore, FakeS3]:
    fake = FakeS3()
    return SessionStore(bucket="test-bucket", client=fake), fake


def make_spec() -> Spec:
    return Spec(
        session_id="sess-001",
        meeting=Meeting(id="123456789", pwd="s3cret", zak="zak-token-xyz"),
        participant_count=2,
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
            RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
        ],
        turns=Turns(seed=4711, windows=[TurnWindow(0.0, 6.4, "10.0.1.119")]),
        timing=Timing(preroll_s=3.5, duration_s=120.0, postroll_s=2.0,
                      join_delay_s={"10.0.1.119": 0.0, "10.0.2.67": 3.2}),
        seeds=Seeds(turns=4711, timing=9001),
    )


# --- key layout ------------------------------------------------------------ #

def test_key_layout():
    assert SessionStore.spec_key("s1") == "sessions/s1/spec.json"
    assert SessionStore.heartbeat_key("s1", "10.0.1.119") == "sessions/s1/heartbeats/10.0.1.119.json"
    assert SessionStore.capture_key("s1") == "sessions/s1/capture.pcap"
    assert SessionStore.manifest_key("s1") == "sessions/s1/manifest.json"


# --- spec publish / read --------------------------------------------------- #

def test_publish_then_read_spec_round_trip():
    store, _ = make_store()
    spec = make_spec()
    store.publish_spec(spec)
    assert store.read_spec("sess-001") == spec


def test_spec_exists():
    store, _ = make_store()
    assert store.spec_exists("sess-001") is False
    store.publish_spec(make_spec())
    assert store.spec_exists("sess-001") is True


def test_published_spec_is_valid_json_at_expected_key():
    store, fake = make_store()
    store.publish_spec(make_spec())
    blob = fake.objects["sessions/sess-001/spec.json"]
    assert json.loads(blob)["session_id"] == "sess-001"


# --- heartbeats ------------------------------------------------------------ #

def test_write_and_read_all_heartbeats_sorted():
    store, _ = make_store()
    store.write_heartbeats("sess-001", "10.0.2.67", [
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.2.67", 103.2),
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.2.67", 219.0),
    ])
    store.write_heartbeats("sess-001", "10.0.1.119", [
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.1.119", 100.0),
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.1.119", 220.0),
    ])
    events = store.read_all_heartbeats("sess-001")
    # Flattened across both files and sorted by timestamp.
    assert [e.ts for e in events] == [100.0, 103.2, 219.0, 220.0]


def test_read_heartbeats_empty_when_none():
    store, _ = make_store()
    assert store.read_all_heartbeats("sess-001") == []


# --- manifest -------------------------------------------------------------- #

def test_write_manifest():
    store, fake = make_store()
    manifest = Manifest(
        session_id="sess-001", meeting_id="123456789",
        roster=[RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST)],
        joins_leaves=[], capture=Capture(1.0, 2.0, "sessions/sess-001/capture.pcap"),
        seeds=Seeds(turns=4711, timing=9001),
    )
    store.write_manifest(manifest)
    stored = json.loads(fake.objects["sessions/sess-001/manifest.json"])
    assert stored["meeting_id"] == "123456789"
    assert "pwd" not in stored and "zak" not in stored


# --- session listing ------------------------------------------------------- #

def test_list_session_ids():
    store, _ = make_store()
    store.publish_spec(make_spec())
    other = make_spec()
    other.session_id = "sess-002"
    store.publish_spec(other)
    assert store.list_session_ids() == ["sess-001", "sess-002"]


# --- binary upload / download (capture, audio) ----------------------------- #

def test_upload_capture_and_audio_round_trip(tmp_path):
    store, _ = make_store()
    pcap = tmp_path / "capture.pcap"
    pcap.write_bytes(b"\xd4\xc3\xb2\xa1fake-pcap")
    key = store.upload_capture("sess-001", str(pcap))
    assert key == "sessions/sess-001/capture.pcap"

    out = tmp_path / "downloaded.pcap"
    store.download_audio_source(str(out), key=key)
    assert out.read_bytes() == b"\xd4\xc3\xb2\xa1fake-pcap"