"""Checkpoint tests for seeded session timing (orchestrator/timing.py).

Pure, local, no AWS. Run with:  pytest tests/test_timing.py
"""

import pytest

from orchestrator.timing import generate_timing

CLIENTS = ["10.0.1.119", "10.0.2.67"]


def test_reproducible_from_seed():
    a = generate_timing(9001, CLIENTS)
    b = generate_timing(9001, CLIENTS)
    assert a == b


def test_different_seeds_differ():
    a = generate_timing(1, CLIENTS)
    b = generate_timing(2, CLIENTS)
    assert (a.preroll_s, a.duration_s, a.postroll_s) != (b.preroll_s, b.duration_s, b.postroll_s)


def test_one_join_delay_per_client():
    t = generate_timing(5, CLIENTS)
    assert set(t.join_delay_s) == set(CLIENTS)


def test_join_offsets_much_smaller_than_duration():
    """Section 6 rule: everyone must be on the call well before it ends."""
    for seed in range(50):
        t = generate_timing(seed, CLIENTS)
        latest_join = max(t.join_delay_s.values())
        # Capped to a quarter of the duration by default -> long N-party overlap.
        assert latest_join <= t.duration_s * 0.25 + 1e-6
        assert latest_join < t.duration_s


def test_values_within_bounds():
    for seed in range(50):
        t = generate_timing(seed, CLIENTS)
        assert 2.0 <= t.preroll_s <= 8.0
        assert 60.0 <= t.duration_s <= 180.0
        assert 2.0 <= t.postroll_s <= 8.0
        assert all(d >= 0.0 for d in t.join_delay_s.values())


def test_join_window_absolute_ceiling():
    """Even for long calls, the join ramp stays under the absolute ceiling."""
    t = generate_timing(3, CLIENTS, duration_range_s=(600.0, 600.0), max_join_delay_s=10.0)
    assert max(t.join_delay_s.values()) <= 10.0


def test_custom_bounds_respected():
    t = generate_timing(7, CLIENTS, duration_range_s=(30.0, 30.0))
    assert t.duration_s == 30.0


def test_rejects_no_clients():
    with pytest.raises(ValueError):
        generate_timing(1, [])


def test_rejects_bad_range():
    with pytest.raises(ValueError):
        generate_timing(1, CLIENTS, preroll_range_s=(8.0, 2.0))


def test_rejects_bad_fraction():
    with pytest.raises(ValueError):
        generate_timing(1, CLIENTS, join_window_fraction=1.5)