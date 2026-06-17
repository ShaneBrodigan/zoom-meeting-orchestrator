"""Checkpoint tests for the bulk generation plan (orchestrator/generation_plan.py).

Pure, local, no AWS. Covers the two things that would silently corrupt the dataset if
wrong: the config validation (a bad weight / jitter must fail loudly, not skew the data)
and the balanced, reproducible sampling (exact per-batch quotas, seed reproducibility,
valid participant/host picks). Run with:  pytest tests/test_generation_plan.py
"""

import json
import random
from collections import Counter

import pytest

from orchestrator.generation_plan import (
    DurationPolicy,
    GenerationPlan,
    PartySizePolicy,
    _balanced_schedule,
    load_generation_plan,
)

CLIENTS = ["10.0.1.119", "10.0.2.67", "10.0.3.53"]


def make_plan(*, duration_weights=None, party_weights=None, jitter_pct=0.10,
              buckets=None, sizes=None, min_duration_s=30.0) -> GenerationPlan:
    buckets = buckets if buckets is not None else [5, 15, 20, 30]
    sizes = sizes if sizes is not None else [2, 3]
    return GenerationPlan(
        duration=DurationPolicy(
            buckets_min=buckets,
            weights=duration_weights if duration_weights is not None else [0.25] * len(buckets),
            jitter_pct=jitter_pct,
            min_duration_s=min_duration_s,
        ),
        party_size=PartySizePolicy(
            sizes=sizes,
            weights=party_weights if party_weights is not None else [1.0 / len(sizes)] * len(sizes),
        ),
    )


# --- validation: bad configs fail loudly ----------------------------------- #

def test_duration_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        make_plan(duration_weights=[0.25, 0.25, 0.25, 0.30])


def test_duration_weights_length_must_match_buckets():
    with pytest.raises(ValueError, match="match the number"):
        make_plan(duration_weights=[0.5, 0.5])


def test_negative_weight_rejected():
    with pytest.raises(ValueError, match=">= 0"):
        make_plan(duration_weights=[0.5, 0.6, -0.1, 0.0])


def test_jitter_out_of_range_rejected():
    with pytest.raises(ValueError, match="jitter_pct"):
        make_plan(jitter_pct=1.0)


def test_jitter_below_floor_rejected():
    # 5 min - 60% = 2 min = 120s, fine; but with a 30-min smallest and a huge floor it trips.
    with pytest.raises(ValueError, match="floor"):
        make_plan(buckets=[1], duration_weights=[1.0], jitter_pct=0.5, min_duration_s=60.0)


def test_party_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1.0"):
        make_plan(party_weights=[0.4, 0.4])


def test_empty_buckets_rejected():
    with pytest.raises(ValueError, match="at least one bucket"):
        make_plan(buckets=[], duration_weights=[])


# --- range_for: bucket -> seconds with jitter ------------------------------ #

def test_range_for_applies_jitter():
    plan = make_plan(jitter_pct=0.10)
    lo, hi = plan.duration.range_for(15)
    assert lo == pytest.approx(15 * 60 * 0.9)   # 810.0
    assert hi == pytest.approx(15 * 60 * 1.1)   # 990.0


# --- build: reproducibility ------------------------------------------------ #

def test_build_reproducible_from_seed():
    plan = make_plan()
    a = plan.build(12, 4711, CLIENTS)
    b = plan.build(12, 4711, CLIENTS)
    assert a == b


def test_build_differs_with_seed():
    plan = make_plan()
    a = plan.build(12, 1, CLIENTS)
    b = plan.build(12, 2, CLIENTS)
    assert a != b


def test_build_count_honoured():
    plan = make_plan()
    assert len(plan.build(7, 5, CLIENTS)) == 7


# --- build: exact quota balance (the point of "quota + shuffle") ----------- #

def test_duration_buckets_exactly_balanced():
    plan = make_plan()  # 4 buckets, equal weights
    plans = plan.build(20, 99, CLIENTS)  # 20 / 4 = 5 each
    counts = Counter(p.duration_bucket_min for p in plans)
    assert counts == {5: 5, 15: 5, 20: 5, 30: 5}


def test_party_sizes_follow_weights_exactly():
    plan = make_plan(party_weights=[0.25, 0.75])  # sizes [2, 3]
    plans = plan.build(20, 7, CLIENTS)
    counts = Counter(len(p.participant_ips) for p in plans)
    assert counts == {2: 5, 3: 15}


def test_balanced_schedule_exact_length_under_weight_drift():
    # Weights summing a hair above 1.0 (still within the load tolerance) over-allocate via
    # float drift at very large counts. The schedule must still be exactly `count` long —
    # before the claw-back fix this produced count+1 entries, silently dropping a session.
    count = 1_000_000
    sched = _balanced_schedule([2, 3], [0.5, 0.500001], count, random.Random(0))
    assert len(sched) == count


def test_largest_remainder_sums_to_count_when_not_divisible():
    plan = make_plan()  # 4 equal buckets
    plans = plan.build(10, 3, CLIENTS)  # 10 / 4 -> 3,3,2,2 in some order
    counts = Counter(p.duration_bucket_min for p in plans)
    assert sum(counts.values()) == 10
    assert sorted(counts.values()) == [2, 2, 3, 3]


# --- build: participant / host picks are valid ----------------------------- #

def test_participants_match_party_size_and_are_distinct():
    plan = make_plan()
    for p in plan.build(30, 11, CLIENTS):
        assert len(p.participant_ips) == len(set(p.participant_ips))  # distinct
        assert set(p.participant_ips) <= set(CLIENTS)                  # real subnets only


def test_host_is_one_of_the_participants():
    plan = make_plan()
    for p in plan.build(30, 11, CLIENTS):
        assert p.host_ip in p.participant_ips


def test_two_party_calls_use_varied_subnet_pairs():
    # Random 2-of-3 should, over a batch, exercise more than one pair (not pinned to VM1+VM2).
    plan = make_plan(party_weights=[1.0, 0.0])  # all 2-party
    pairs = {frozenset(p.participant_ips) for p in plan.build(30, 5, CLIENTS)}
    assert len(pairs) >= 2


def test_per_session_seeds_vary():
    plan = make_plan()
    timing_seeds = {p.seeds.timing for p in plan.build(20, 5, CLIENTS)}
    assert len(timing_seeds) > 1


# --- build: cross-checks against the client pool --------------------------- #

def test_rejects_party_size_larger_than_client_pool():
    plan = make_plan(sizes=[2, 3], party_weights=[0.5, 0.5])
    with pytest.raises(ValueError, match="client IPs"):
        plan.build(4, 1, ["10.0.1.119", "10.0.2.67"])  # only 2 clients, but 3-party requested


def test_rejects_nonpositive_count():
    plan = make_plan()
    with pytest.raises(ValueError, match="count"):
        plan.build(0, 1, CLIENTS)


def test_rejects_duplicate_client_ips():
    plan = make_plan()
    with pytest.raises(ValueError, match="distinct"):
        plan.build(4, 1, ["10.0.1.119", "10.0.1.119", "10.0.2.67"])


# --- load from file -------------------------------------------------------- #

def test_load_generation_plan_round_trips(tmp_path):
    blob = {
        "duration": {"buckets_min": [5, 15, 20, 30], "weights": [0.25, 0.25, 0.25, 0.25],
                     "jitter_pct": 0.10, "min_duration_s": 30},
        "party_size": {"sizes": [2, 3], "weights": [0.5, 0.5]},
    }
    path = tmp_path / "generation_plan.json"
    path.write_text(json.dumps(blob), encoding="utf-8")
    plan = load_generation_plan(str(path))
    assert plan.duration.buckets_min == [5, 15, 20, 30]
    assert plan.party_size.sizes == [2, 3]
    # And it actually builds.
    assert len(plan.build(8, 1, CLIENTS)) == 8


def test_committed_config_is_valid():
    """The repo's config/generation_plan.json must load and build (it ships the defaults)."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plan = load_generation_plan(os.path.join(here, "config", "generation_plan.json"))
    assert plan.build(4, 1, CLIENTS)
