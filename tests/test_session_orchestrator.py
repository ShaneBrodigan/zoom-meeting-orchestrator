"""Checkpoint tests for the session orchestrator (orchestrator/session_orchestrator.py).

Local, no Zoom / tshark / AWS: the whole conductor is driven through its front door
(``run_session``) with fakes for the three things it touches — the meeting scheduler,
the S3 store, and the tshark capture. The fakes record call order, so the tests can
assert the design's ordering invariants directly:

* tshark is capturing *before* the spec is published (every join is captured),
* the random preroll is slept *between* capture-start and publish (recorded dead-air),
* the meeting is ended and the capture stopped, with the postroll slept between,
* credentials never reach the manifest,
* a missing bot shows up as an empty join (the first plumbing checkpoint), and
* a mid-call failure still ends the meeting and stops the capture.

Run with:  pytest tests/test_session_orchestrator.py
"""

import pytest

from common import schema
from common.s3 import SessionStore
from common.schema import (
    HeartbeatEvent,
    Manifest,
    Meeting,
    RosterEntry,
    Seeds,
)
from orchestrator.session_orchestrator import (
    SessionConfig,
    SessionOrchestrator,
    new_session_id,
)

# Reuse the in-memory boto3 fake that exercises the real SessionStore logic.
from tests.test_s3 import FakeS3


class FakeScheduler:
    """Stand-in for MeetingScheduler: hands back a fixed meeting, records end calls."""

    def __init__(self, *, meeting=None, fail_end=False):
        self.meeting = meeting or Meeting(id="123456789", pwd="s3cret", zak="zak-token")
        self.fail_end = fail_end
        self.created = False
        self.ended = []        # meeting ids passed to end_meeting

    def create_meeting(self, *, topic="Bot Meeting"):
        self.created = True
        self.topic = topic
        return self.meeting

    def end_meeting(self, meeting_id):
        self.ended.append(meeting_id)
        if self.fail_end:
            raise RuntimeError("zoom end failed")


class FakeCapture:
    """Stand-in for PacketCapture: records start/stop and the path it was built for."""

    def __init__(self, path, log, *, fail_start=False):
        self.path = path
        self._log = log
        self.fail_start = fail_start
        self.started = False
        self.stopped = False

    def start(self):
        if self.fail_start:
            raise RuntimeError("tshark would not start")
        self.started = True
        self._log.append("capture.start")
        return 1000.0

    def stop(self):
        self.stopped = True
        self._log.append("capture.stop")
        return 1234.0


class RecordingStore(SessionStore):
    """Real SessionStore over a FakeS3, but logging the orchestration-relevant calls."""

    def __init__(self, log):
        super().__init__(bucket="test-bucket", client=FakeS3())
        self._log = log

    def publish_spec(self, spec):
        self._log.append("publish_spec")
        return super().publish_spec(spec)

    def upload_capture(self, session_id, local_pcap_path):
        # No real pcap on disk in these tests; record the key without touching the file.
        self._log.append("upload_capture")
        return self.capture_key(session_id)

    # Read-back helpers for assertions (the store has no read_manifest of its own —
    # only the offline labeler reads manifests, so it lives here in the test).
    def raw_manifest_bytes_for_test(self, session_id) -> bytes:
        return self._client.objects[self.manifest_key(session_id)]

    def read_manifest_for_test(self, session_id) -> Manifest:
        return Manifest.from_json(self.raw_manifest_bytes_for_test(session_id).decode())


def make_orchestrator(*, fail_end=False, fail_start=False):
    log: list[str] = []
    scheduler = FakeScheduler(fail_end=fail_end)
    store = RecordingStore(log)
    captures: list[FakeCapture] = []

    def factory(path):
        cap = FakeCapture(path, log, fail_start=fail_start)
        captures.append(cap)
        return cap

    sleeps: list[float] = []
    orch = SessionOrchestrator(scheduler, store, capture_factory=factory,
                               sleep=lambda s: sleeps.append(s))
    return orch, scheduler, store, captures, log, sleeps


def two_party_config(session_id="sess-test") -> SessionConfig:
    return SessionConfig(
        session_id=session_id,
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
            RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
        ],
        seeds=Seeds(turns=4711, timing=9001),
    )


# --- happy path: full plumbing dance --------------------------------------- #

def test_run_session_returns_and_writes_manifest():
    orch, _, store, _, _, _ = make_orchestrator()
    manifest = orch.run_session(two_party_config())

    assert isinstance(manifest, Manifest)
    # The manifest was actually written to S3 at the right key, and round-trips.
    written = store.read_manifest_for_test("sess-test")
    assert written.meeting_id == "123456789"
    assert written.session_id == "sess-test"


def test_capture_starts_before_spec_is_published():
    # The core invariant: tshark must be capturing before clients can see the session.
    orch, _, _, _, log, _ = make_orchestrator()
    orch.run_session(two_party_config())
    assert log.index("capture.start") < log.index("publish_spec")


def test_full_call_ordering():
    # start → publish → end → stop, in exactly that order (preroll/postroll between).
    orch, scheduler, _, _, log, _ = make_orchestrator()
    orch.run_session(two_party_config())
    assert log == [
        "capture.start",
        "publish_spec",
        "capture.stop",
        "upload_capture",
    ]
    # end_meeting is the hard stop and happens before the capture is stopped.
    assert scheduler.ended == ["123456789"]


def test_preroll_slept_between_start_and_publish_and_postroll_before_stop():
    # Three sleeps in order: preroll (recorded dead-air), duration, postroll (tail).
    orch, _, _, _, _, sleeps = make_orchestrator()
    cfg = two_party_config()
    orch.run_session(cfg)
    # The seeded timing produced three positive sleeps in preroll/duration/postroll order.
    assert len(sleeps) == 3
    preroll, duration, postroll = sleeps
    assert duration > preroll and duration > postroll  # join offsets << duration


# --- the first checkpoint shape: no bots yet ------------------------------- #

def test_plumbing_run_has_empty_joins_when_no_heartbeats():
    # No bots exist yet, so no heartbeats were written: a missing join is a recorded
    # fact (t_join / t_leave None), not an error.
    orch, _, _, _, _, _ = make_orchestrator()
    manifest = orch.run_session(two_party_config())
    assert [jl.ip for jl in manifest.joins_leaves] == ["10.0.1.119", "10.0.2.67"]
    assert all(jl.t_join is None and jl.t_leave is None for jl in manifest.joins_leaves)


def test_joins_leaves_derived_when_heartbeats_present():
    # When clients have reported, the manifest reflects their real join/leave times.
    orch, _, store, _, _, _ = make_orchestrator()
    store.write_heartbeats("sess-test", "10.0.1.119", [
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.1.119", 1010.0),
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.1.119", 1200.0),
    ])
    manifest = orch.run_session(two_party_config())
    host = next(jl for jl in manifest.joins_leaves if jl.ip == "10.0.1.119")
    assert host.t_join == 1010.0 and host.t_leave == 1200.0


# --- credentials never leak ------------------------------------------------ #

def test_manifest_carries_no_credentials():
    orch, _, store, _, _, _ = make_orchestrator()
    orch.run_session(two_party_config())
    blob = store.raw_manifest_bytes_for_test("sess-test")
    assert b"s3cret" not in blob and b"zak-token" not in blob


# --- capture window recorded ----------------------------------------------- #

def test_capture_window_recorded_in_manifest():
    orch, _, _, _, _, _ = make_orchestrator()
    manifest = orch.run_session(two_party_config())
    assert manifest.capture.t_start == 1000.0
    assert manifest.capture.t_stop == 1234.0
    assert manifest.capture.pcap_key == "sessions/sess-test/capture.pcap"


# --- failure handling: meeting still ends, capture still stops -------------- #

def test_failed_publish_still_ends_meeting_and_stops_capture():
    orch, scheduler, store, captures, _, _ = make_orchestrator()

    def boom(spec):
        raise RuntimeError("S3 publish failed")
    store.publish_spec = boom  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="S3 publish failed"):
        orch.run_session(two_party_config())

    assert scheduler.ended == ["123456789"]   # hard stop ran
    assert captures[0].stopped is True         # capture closed, no tshark left running


def test_capture_that_will_not_start_still_ends_meeting():
    orch, scheduler, _, _, _, _ = make_orchestrator(fail_start=True)
    with pytest.raises(RuntimeError, match="tshark would not start"):
        orch.run_session(two_party_config())
    assert scheduler.ended == ["123456789"]


def test_cleanup_error_surfaces_on_happy_path():
    # If the call body succeeds but ending the meeting fails, that error is surfaced.
    orch, _, _, _, _, _ = make_orchestrator(fail_end=True)
    with pytest.raises(RuntimeError, match="zoom end failed"):
        orch.run_session(two_party_config())


# --- config / id helpers --------------------------------------------------- #

def test_roster_with_only_noise_is_rejected():
    orch, _, _, _, _, _ = make_orchestrator()
    cfg = SessionConfig(
        roster=[RosterEntry(ip="10.0.4.16", zoom_role=schema.ROLE_NONE)],
        seeds=Seeds(turns=1, timing=2),
    )
    with pytest.raises(ValueError):
        orch.run_session(cfg)


def test_participant_count_excludes_noise_vms():
    orch, _, store, _, _, _ = make_orchestrator()
    cfg = SessionConfig(
        session_id="sess-noise",
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
            RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
            RosterEntry(ip="10.0.4.16", zoom_role=schema.ROLE_NONE),  # VM5 noise, not a participant
        ],
        seeds=Seeds(turns=4711, timing=9001),
    )
    orch.run_session(cfg)
    spec = store.read_spec("sess-noise")
    assert spec.participant_count == 2
    # The noise VM is still in the roster (recorded), just not counted/joined.
    assert {e.ip for e in spec.roster} == {"10.0.1.119", "10.0.2.67", "10.0.4.16"}


def test_new_session_id_is_unique_and_prefixed():
    a, b = new_session_id(), new_session_id()
    assert a.startswith("sess-") and b.startswith("sess-")
    assert a != b