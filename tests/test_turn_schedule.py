"""Checkpoint tests for the seeded turn schedule (orchestrator/turn_schedule.py).

Pure, local, no AWS. Run with:  pytest tests/test_turn_schedule.py
"""

import pytest

from orchestrator.turn_schedule import (
    LONG_PAUSE_MIN_S,
    LONG_PAUSE_MAX_S,
    DEFAULT_MAX_TURN_S,
    generate_turns,
)

SPEAKERS = ["10.0.1.119", "10.0.2.67"]


def _intersect(a, b):
    """True if two windows overlap in time."""
    return a.t0 < b.t1 and b.t0 < a.t1


def _contained(inner, outer):
    """True if ``inner`` sits wholly inside ``outer`` (a backchannel shape)."""
    return outer.t0 <= inner.t0 and inner.t1 <= outer.t1


def test_reproducible_from_seed():
    """Same seed + inputs -> identical schedule."""
    a = generate_turns(4711, SPEAKERS, duration_s=60.0)
    b = generate_turns(4711, SPEAKERS, duration_s=60.0)
    assert a == b
    assert a.seed == 4711


def test_different_seeds_differ():
    a = generate_turns(1, SPEAKERS, duration_s=60.0)
    b = generate_turns(2, SPEAKERS, duration_s=60.0)
    assert a.windows != b.windows


def test_windows_are_chronological_and_positive_length():
    # Windows are sorted by start time and each has positive length. They MAY overlap
    # now (brief double-talk, backchannels) — that is tested separately below.
    turns = generate_turns(99, SPEAKERS, duration_s=120.0)
    for prev, cur in zip(turns.windows, turns.windows[1:]):
        assert prev.t0 <= cur.t0, "windows must be in start-time order"
    for w in turns.windows:
        assert w.t0 < w.t1, "each window has positive length"


def test_covers_and_does_not_exceed_duration():
    duration = 90.0
    turns = generate_turns(7, SPEAKERS, duration_s=duration)
    assert turns.windows, "schedule should not be empty"
    assert turns.windows[0].t0 == 0.0
    assert all(w.t1 <= duration for w in turns.windows)
    # The schedule should reach near the end, not stop early. A trailing long pause can
    # leave dead air up to LONG_PAUSE_MAX_S + a turn short of the end, so allow for that.
    assert turns.windows[-1].t1 >= duration - LONG_PAUSE_MAX_S - DEFAULT_MAX_TURN_S


def test_long_pauses_occur():
    # Over a long call, the 10% long-pause event should produce at least one big gap.
    turns = generate_turns(99, SPEAKERS, duration_s=600.0)
    gaps = [cur.t0 - prev.t1 for prev, cur in zip(turns.windows, turns.windows[1:])]
    assert max(gaps) >= LONG_PAUSE_MIN_S, "expected at least one 4-6s thinking pause"


def test_overlaps_and_backchannels_occur():
    # Multi-party calls must produce moments where two speakers are active at once:
    # both a brief handover overlap and a fully-contained backchannel interjection.
    turns = generate_turns(99, SPEAKERS, duration_s=600.0)
    ws = turns.windows
    pairs = [(a, b) for i, a in enumerate(ws) for b in ws[i + 1:] if _intersect(a, b)]
    assert pairs, "expected overlapping windows (double-talk / backchannels)"
    assert any(a.speaker != b.speaker for a, b in pairs), "overlaps are between speakers"
    assert any(_contained(b, a) or _contained(a, b) for a, b in pairs), \
        "expected a backchannel: one window wholly inside another"


def test_single_speaker_never_overlaps():
    # Overlaps and backchannels need a second person, so a solo call stays half-duplex.
    turns = generate_turns(5, ["10.0.1.119"], duration_s=600.0)
    for prev, cur in zip(turns.windows, turns.windows[1:]):
        assert prev.t1 <= cur.t0, "single-speaker schedule must not overlap"


def test_all_speakers_drawn_from_input():
    turns = generate_turns(55, SPEAKERS, duration_s=300.0)
    assert {w.speaker for w in turns.windows} <= set(SPEAKERS)


def test_single_speaker_allowed():
    turns = generate_turns(5, ["10.0.1.119"], duration_s=30.0)
    assert all(w.speaker == "10.0.1.119" for w in turns.windows)


def test_rejects_no_speakers():
    with pytest.raises(ValueError):
        generate_turns(1, [], duration_s=30.0)


def test_rejects_bad_duration():
    with pytest.raises(ValueError):
        generate_turns(1, SPEAKERS, duration_s=0)


def test_rejects_bad_turn_bounds():
    with pytest.raises(ValueError):
        generate_turns(1, SPEAKERS, duration_s=30.0, min_turn_s=10.0, max_turn_s=5.0)