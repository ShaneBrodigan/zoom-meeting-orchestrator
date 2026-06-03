"""Checkpoint tests for the seeded turn schedule (orchestrator/turn_schedule.py).

Pure, local, no AWS. Run with:  pytest tests/test_turn_schedule.py
"""

import pytest

from orchestrator.turn_schedule import generate_turns

SPEAKERS = ["10.0.1.119", "10.0.2.67"]


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


def test_windows_are_ordered_and_non_overlapping():
    turns = generate_turns(99, SPEAKERS, duration_s=120.0)
    for prev, cur in zip(turns.windows, turns.windows[1:]):
        assert prev.t1 <= cur.t0, "windows must not overlap (half-duplex)"
        assert prev.t0 < prev.t1, "each window has positive length"


def test_covers_and_does_not_exceed_duration():
    duration = 90.0
    turns = generate_turns(7, SPEAKERS, duration_s=duration)
    assert turns.windows, "schedule should not be empty"
    assert turns.windows[0].t0 == 0.0
    assert turns.windows[-1].t1 <= duration
    # The schedule should reach near the end, not stop early.
    assert turns.windows[-1].t1 >= duration - 1.0


def test_no_immediate_repeat_with_multiple_speakers():
    turns = generate_turns(123, SPEAKERS, duration_s=300.0)
    for prev, cur in zip(turns.windows, turns.windows[1:]):
        assert prev.speaker != cur.speaker


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