"""Checkpoint tests for the Spec/Manifest contract (common/schema.py).

Pure, local, no AWS. Run with:  pytest tests/test_schema.py
"""

import json

import pytest

from common import schema
from common.schema import (
    Capture,
    HeartbeatEvent,
    JoinLeave,
    Manifest,
    Meeting,
    NoiseBlock,
    RosterEntry,
    Seeds,
    Spec,
    Timing,
    Turns,
    TurnWindow,
)


def make_spec() -> Spec:
    """A representative 2-party + VM5-noise spec, mirroring REFACTOR_DESIGN.md section 3."""
    return Spec(
        session_id="sess-001",
        meeting=Meeting(id="123456789", pwd="s3cret", zak="zak-token-xyz"),
        participant_count=2,
        media_profile=schema.MEDIA_AUDIO,
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
            RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
            RosterEntry(
                ip="10.0.4.16",
                zoom_role=schema.ROLE_NONE,
                noise=NoiseBlock(
                    enabled=True, profile="iperf", target="10.0.0.7",
                    ports="5201", intensity="medium",
                ),
            ),
        ],
        turns=Turns(
            seed=4711,
            windows=[
                TurnWindow(t0=0.0, t1=6.4, speaker="10.0.1.119"),
                TurnWindow(t0=6.4, t1=12.0, speaker="10.0.2.67"),
            ],
        ),
        timing=Timing(
            preroll_s=3.5, duration_s=120.0, postroll_s=2.0,
            join_delay_s={"10.0.1.119": 0.0, "10.0.2.67": 3.2},
        ),
        seeds=Seeds(turns=4711, timing=9001),
    )


def make_manifest() -> Manifest:
    return Manifest(
        session_id="sess-001",
        meeting_id="123456789",
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
            RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
        ],
        joins_leaves=[
            JoinLeave(ip="10.0.1.119", t_join=100.0, t_leave=220.0),
            JoinLeave(ip="10.0.2.67", t_join=103.2, t_leave=220.0),
        ],
        capture=Capture(t_start=96.5, t_stop=222.0, pcap_key="sessions/sess-001/capture.pcap"),
        audio={"seed": 4711, "source": "librispeech_audio.pcm"},
        noise={"enabled": False},
        seeds=Seeds(turns=4711, timing=9001),
    )


# --- round-trip: nothing gets mangled through JSON ------------------------- #

def test_spec_round_trip():
    spec = make_spec()
    assert Spec.from_json(spec.to_json()) == spec


def test_manifest_round_trip():
    manifest = make_manifest()
    assert Manifest.from_json(manifest.to_json()) == manifest


def test_heartbeat_round_trip():
    hb = HeartbeatEvent(event=schema.EVENT_JOINED, ip="10.0.1.119", ts=100.0)
    assert HeartbeatEvent.from_dict(hb.to_dict()) == hb


# --- the credential-redaction guarantee ------------------------------------ #

def test_manifest_cannot_carry_credentials():
    """The password and host token must never appear anywhere in a manifest."""
    manifest = make_manifest()
    blob = manifest.to_json()
    assert "s3cret" not in blob
    assert "zak-token-xyz" not in blob
    # And there is structurally no field to put them in.
    assert "pwd" not in manifest.to_dict()
    assert "zak" not in manifest.to_dict()


def test_spec_keeps_credentials_for_runtime():
    """The runtime spec, by contrast, must carry the credentials clients join with."""
    blob = make_spec().to_json()
    assert "s3cret" in blob
    assert "zak-token-xyz" in blob


# --- client self-identification by IP -------------------------------------- #

def test_entry_for_ip_finds_own_role():
    spec = make_spec()
    assert spec.entry_for_ip("10.0.1.119").zoom_role == schema.ROLE_HOST
    assert spec.entry_for_ip("10.0.2.67").zoom_role == schema.ROLE_JOINER
    assert spec.entry_for_ip("10.0.4.16").noise.enabled is True


def test_entry_for_ip_unknown_returns_none():
    assert make_spec().entry_for_ip("10.9.9.9") is None


# --- hard-to-misuse: invalid enum values are rejected ---------------------- #

def test_invalid_zoom_role_rejected():
    with pytest.raises(ValueError):
        RosterEntry(ip="10.0.1.1", zoom_role="captain")


def test_invalid_media_profile_rejected():
    spec = make_spec()
    with pytest.raises(ValueError):
        Spec.from_dict({**spec.to_dict(), "media_profile": "hologram"})


def test_invalid_heartbeat_event_rejected():
    with pytest.raises(ValueError):
        HeartbeatEvent(event="exploded", ip="10.0.1.1", ts=1.0)


# --- the version stamp travels with the document --------------------------- #

def test_schema_version_present():
    assert json.loads(make_spec().to_json())["schema_version"] == schema.SCHEMA_VERSION
    assert json.loads(make_manifest().to_json())["schema_version"] == schema.SCHEMA_VERSION