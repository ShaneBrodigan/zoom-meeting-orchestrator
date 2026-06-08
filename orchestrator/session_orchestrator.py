"""Run one capture session end to end on VM4 (the conductor).

This is the old ``sample_program/CallSpawner`` reborn for the distributed harness —
but with the one job it must NOT do removed: it never joins Zoom and never forks
bots. The clients do that themselves when they see the spec (REFACTOR_DESIGN.md
decision 9). VM4's job is purely to *conduct*: create the meeting, capture the
wire, publish the spec that lets clients in, end the meeting, and write down what
happened.

It owns no new logic of its own — it composes the already-built, already-verified
pieces in the one correct order:

    create meeting (REST)        meeting_scheduler.MeetingScheduler
    pick seeded timing/turns     timing.generate_timing, turn_schedule.generate_turns
    start tshark                 capture.PacketCapture   (blocks until really capturing)
    publish spec.json            common.s3.SessionStore
    end meeting (REST)           meeting_scheduler.MeetingScheduler  (hard media stop)
    stop tshark, build manifest  capture.PacketCapture, manifest.build_manifest

The ordering encodes the design's realism decisions, settled with Shane in a
grill-me pass:

* **Recorded preroll (decision 4 / 5).** tshark is started *first*, then a random
  preroll is slept, *then* the spec is published. So the pcap literally begins with
  a random stretch of quiet-with-background before anyone can join — not an
  unrecorded pause. (Clients cannot see the session until the spec exists, so this
  also guarantees every join is captured.)
* **Recorded postroll tail.** After the meeting is ended (the hard media stop), a
  random postroll is slept *before* tshark is stopped, so the pcap ends with the
  call winding down to background-only rather than cutting off mid-packet.
* **Noise is a record, not a trigger.** Background traffic (VM5 iperf) runs
  independently of the session, so it fills the preroll/postroll and gaps. The
  spec's ``noise`` block *records* what VM5 is doing for flow separability; this
  orchestrator never starts or stops it.

Everything the conductor touches is injected (scheduler, S3 store, a capture
factory, a ``sleep``), exactly like the other modules inject boto3 / requests /
the process launcher — so ``run_session`` can be driven end to end by a test with
fakes, no real Zoom, tshark, or AWS. The first live checkpoint is deliberately a
*plumbing* test: with no bots yet, the manifest's ``joins_leaves`` come back empty
(a missing join is itself a recorded fact), which proves the dance works before
the bots that fill it exist.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from common.s3 import AUDIO_SOURCE_KEY, SessionStore
from common.schema import (
    MEDIA_AUDIO,
    ROLE_NONE,
    Capture,
    Manifest,
    RosterEntry,
    Seeds,
    Spec,
)
from orchestrator.capture import PacketCapture
from orchestrator.manifest import build_manifest
from orchestrator.meeting_scheduler import MeetingScheduler
from orchestrator.timing import generate_timing
from orchestrator.turn_schedule import generate_turns

# dumpcap drops privileges before opening the -w file, so the pcap must live in a
# world-writable dir; /tmp (mode 1777) works, a root-made subdir (755) re-breaks it.
# (Live-verified on VM4, 2026-06-04.)
DEFAULT_PCAP_DIR = "/tmp"


def new_session_id() -> str:
    """A sortable, unique session id like ``sess-20260608T141530Z-9f3a``."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"sess-{stamp}-{secrets.token_hex(2)}"


# A capture factory turns a pcap path into a ready-to-start PacketCapture. Injectable
# so tests can hand back a fake; the default builds a real one on ens5.
CaptureFactory = Callable[[str], PacketCapture]


@dataclass
class SessionConfig:
    """The few things that vary per session; everything else is seeded or default.

    ``roster`` is the single source of truth for who does what: entries whose
    ``zoom_role`` is not ``none`` are the joiners (host + joiners) that get timing,
    turns, and a participant slot; ``none`` entries (e.g. VM5) only carry a noise
    record. ``seeds`` make the timing and turn schedule reproducible from the
    manifest forever.
    """
    roster: list[RosterEntry]
    seeds: Seeds
    session_id: str = field(default_factory=new_session_id)
    media_profile: str = MEDIA_AUDIO
    topic: str = "Bot Meeting"
    pcap_dir: str = DEFAULT_PCAP_DIR
    audio_source: str = AUDIO_SOURCE_KEY


class SessionOrchestrator:
    """VM4's front door: ``run_session(config)`` performs one full capture session."""

    def __init__(self, scheduler: MeetingScheduler, store: SessionStore, *,
                 capture_factory: CaptureFactory | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._scheduler = scheduler
        self._store = store
        self._capture_factory = capture_factory or (lambda path: PacketCapture(path))
        self._sleep = sleep

    @classmethod
    def from_env(cls, *, capture_factory: CaptureFactory | None = None) -> "SessionOrchestrator":
        """Build from the VM4 environment: Zoom S2S creds in .env, IAM-role S3."""
        return cls(MeetingScheduler.from_env(), SessionStore(),
                   capture_factory=capture_factory)

    # --- front door -------------------------------------------------------- #

    def run_session(self, config: SessionConfig) -> Manifest:
        """Conduct one session and return the raw-facts manifest it wrote to S3.

        Creates the meeting, captures the wire across a recorded preroll → call →
        recorded postroll window, publishes the spec that admits clients, ends the
        meeting as a hard media stop, then merges the spec + reported heartbeats +
        capture window into the manifest. The meeting is always ended and the
        capture always stopped, even if the call body fails partway.
        """
        joining_ips = [e.ip for e in config.roster if e.zoom_role != ROLE_NONE]
        if not joining_ips:
            raise ValueError("roster has no joining client (every entry is zoom_role 'none')")

        meeting = self._scheduler.create_meeting(topic=config.topic)
        timing = generate_timing(config.seeds.timing, joining_ips)
        turns = generate_turns(config.seeds.turns, joining_ips, timing.duration_s)
        spec = Spec(
            session_id=config.session_id,
            meeting=meeting,
            participant_count=len(joining_ips),
            roster=config.roster,
            turns=turns,
            timing=timing,
            seeds=config.seeds,
            media_profile=config.media_profile,
        )

        pcap_path = os.path.join(config.pcap_dir, f"{config.session_id}.pcap")
        capture = self._capture_factory(pcap_path)

        t_start: float | None = None
        t_stop: float | None = None
        capture_started = False
        try:
            t_start = capture.start()        # blocks until tshark is really capturing
            capture_started = True
            self._sleep(timing.preroll_s)    # recorded quiet-with-noise before any join
            self._store.publish_spec(spec)   # clients can now see + join the session
            self._sleep(timing.duration_s)   # the call runs; clients join/play/leave
        finally:
            # Always: hard media stop, then a recorded tail, then close the capture —
            # so a failed call still leaves the meeting ended and no tshark running.
            t_stop, cleanup_error = self._shutdown(
                meeting.id, capture, capture_started, timing.postroll_s
            )

        if cleanup_error is not None:
            raise cleanup_error

        pcap_key = self._store.upload_capture(config.session_id, pcap_path)
        heartbeats = self._store.read_all_heartbeats(config.session_id)
        manifest = build_manifest(
            spec,
            heartbeats,
            Capture(t_start=t_start, t_stop=t_stop, pcap_key=pcap_key),
            audio_source=config.audio_source,
        )
        self._store.write_manifest(manifest)
        return manifest

    # --- internals --------------------------------------------------------- #

    def _shutdown(self, meeting_id: str, capture: PacketCapture, capture_started: bool,
                  postroll_s: float) -> tuple[float | None, Exception | None]:
        """End the meeting and close the capture, recording the postroll tail between.

        Both steps are attempted even if one fails, so a flaky meeting-end can never
        leave tshark running into the next session. Returns ``(t_stop, first_error)``;
        the caller re-raises ``first_error`` only when the call body itself succeeded
        (otherwise that original exception is the one worth surfacing).
        """
        error: Exception | None = None
        try:
            self._scheduler.end_meeting(meeting_id)  # hard media stop (decision 5)
        except Exception as err:  # noqa: BLE001 - recorded, not swallowed
            error = err

        t_stop: float | None = None
        if capture_started:
            self._sleep(postroll_s)                  # recorded quiet-with-noise tail
            try:
                t_stop = capture.stop()
            except Exception as err:  # noqa: BLE001
                error = error or err
        return t_stop, error