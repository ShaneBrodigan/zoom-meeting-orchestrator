"""Checkpoint tests for the manifest merge (orchestrator/manifest.py).

Pure, local, no AWS. Run with:  pytest tests/test_manifest.py
"""

from common import schema
from common.schema import (
    Capture,
    HeartbeatEvent,
    Meeting,
    NoiseBlock,
    RosterEntry,
    Seeds,
    Spec,
    Timing,
    Turns,
    TurnWindow,
)
from orchestrator.manifest import build_manifest


def make_spec() -> Spec:
    return Spec(
        session_id="sess-001",
        meeting=Meeting(id="123456789", pwd="s3cret", zak="zak-token-xyz"),
        participant_count=2,
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
            RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
            RosterEntry(
                ip="10.0.4.16",
                zoom_role=schema.ROLE_NONE,
                noise=NoiseBlock(enabled=True, profile="iperf", target="10.0.0.7", ports="5201"),
            ),
        ],
        turns=Turns(seed=4711, windows=[TurnWindow(0.0, 6.4, "10.0.1.119")]),
        timing=Timing(preroll_s=3.5, duration_s=120.0, postroll_s=2.0,
                      join_delay_s={"10.0.1.119": 0.0, "10.0.2.67": 3.2}),
        seeds=Seeds(turns=4711, timing=9001),
    )


def capture() -> Capture:
    return Capture(t_start=96.5, t_stop=222.0, pcap_key="sessions/sess-001/capture.pcap")


def test_meeting_id_only_no_credentials():
    m = build_manifest(make_spec(), [], capture())
    assert m.meeting_id == "123456789"
    blob = m.to_json()
    assert "s3cret" not in blob and "zak-token-xyz" not in blob


def test_joins_leaves_derived_from_heartbeats():
    hbs = [
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.1.119", 100.0),
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.2.67", 103.2),
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.1.119", 220.0),
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.2.67", 219.0),
    ]
    m = build_manifest(make_spec(), hbs, capture())
    by_ip = {jl.ip: jl for jl in m.joins_leaves}
    assert by_ip["10.0.1.119"].t_join == 100.0
    assert by_ip["10.0.1.119"].t_leave == 220.0
    assert by_ip["10.0.2.67"].t_join == 103.2


def test_earliest_join_latest_leave_wins():
    hbs = [
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.1.119", 105.0),
        HeartbeatEvent(schema.EVENT_JOINED, "10.0.1.119", 100.0),  # earlier
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.1.119", 210.0),
        HeartbeatEvent(schema.EVENT_LEFT, "10.0.1.119", 220.0),    # later
    ]
    m = build_manifest(make_spec(), hbs, capture())
    jl = next(j for j in m.joins_leaves if j.ip == "10.0.1.119")
    assert jl.t_join == 100.0
    assert jl.t_leave == 220.0


def test_missing_join_recorded_as_none():
    """A joiner that never reported is a fact: it appears with None, not omitted."""
    m = build_manifest(make_spec(), [], capture())
    by_ip = {jl.ip: jl for jl in m.joins_leaves}
    assert by_ip["10.0.2.67"].t_join is None
    assert by_ip["10.0.2.67"].t_leave is None


def test_noise_vm_excluded_from_joins_leaves():
    m = build_manifest(make_spec(), [], capture())
    assert "10.0.4.16" not in {jl.ip for jl in m.joins_leaves}


def test_noise_summary_records_source():
    m = build_manifest(make_spec(), [], capture())
    assert m.noise["enabled"] is True
    assert m.noise["sources"][0]["ip"] == "10.0.4.16"
    assert m.noise["sources"][0]["profile"] == "iperf"


def test_audio_records_seed_and_optional_source():
    m = build_manifest(make_spec(), [], capture(), audio_source="librispeech_audio.pcm")
    assert m.audio["seed"] == 4711
    assert m.audio["source"] == "librispeech_audio.pcm"


def test_manifest_round_trips_after_build():
    m = build_manifest(make_spec(), [], capture())
    from common.schema import Manifest
    assert Manifest.from_json(m.to_json()) == m