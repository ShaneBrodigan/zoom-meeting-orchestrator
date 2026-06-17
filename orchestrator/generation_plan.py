"""Decide what a *batch* of capture sessions should look like.

The session orchestrator runs **one** session and knows nothing about datasets. This
module owns everything that is about the dataset as a whole: reading the generation
policy from ``config/generation_plan.json`` and turning it, plus a master seed, into a
balanced list of per-session plans for the bulk generator to run. It is pure — no AWS,
no Zoom, no clock — so the dataset-shaping logic can be unit-tested to the last draw.

Two weighted axes, both shaped by the same "exact quota + shuffle" machinery:

* **duration** — a *nuisance* axis. The call length is drawn from a menu of approximate
  buckets (e.g. 5/15/20/30 min), each smeared by ``jitter_pct`` so no exact length
  repeats, and deliberately carries *no* class information (same policy for every
  session). This stops a model keying on flow duration as a shortcut. A bucket becomes a
  per-session ``duration_range_s`` (bucket ± jitter) handed to ``generate_timing``, whose
  existing seeded ``uniform`` draw is the single source of the concrete length — so the
  jitter needs no new randomness and reproduces from the recorded session seed.
* **party_size** — the *class-balance* axis. "2-way vs 3-way" is the participant-count
  label itself, so these weights literally set how many examples of each class the batch
  contains. For each session the participating subnets are chosen at random (e.g. 2 of 3)
  and the host is chosen at random among them, so no subnet is correlated with a class.

"Exact quota" means: across the ``count`` sessions of one batch, each value gets
``round(weight * count)`` sessions (largest-remainder rounding so they sum to exactly
``count``), then the schedule is shuffled. So the proportions are exact within a batch,
not merely "equal in expectation" — the only randomness in *which* value a session gets
is the shuffle order. Across separate batches the balance is approximate; top up from S3
if it drifts.

Everything is driven by the master ``seed`` in a fixed draw order, so a whole batch
reproduces from ``(plan, count, seed)``, and each session additionally reproduces on its
own from the per-session seeds recorded in its manifest.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from typing import Any

from common.schema import Seeds

# Per-session timing/turns seeds are drawn from this space. 2**31 keeps them comfortably
# inside a C int for any downstream tool and well clear of collision at batch sizes.
_SEED_SPACE = 2**31

# Weights are required to sum to 1.0; allow a little float slack (0.45 + 0.30 + 0.25 does
# not land exactly on 1.0 in binary). A typo'd weight is still rejected, not normalized.
_WEIGHT_SUM_TOL = 1e-6


@dataclass
class DurationPolicy:
    """The nuisance-axis policy: a menu of approximate call lengths and their shares.

    ``buckets_min`` are call-body minutes (just the call, not pre/post-roll). ``weights``
    (same length, summing to 1.0) set each bucket's share of the batch. ``jitter_pct`` is
    the ± fraction each bucket is smeared by (0.10 = ±10%, drawn uniformly). The smallest
    possible jittered length, ``min(buckets)*60*(1-jitter_pct)``, must stay at or above
    ``min_duration_s`` — a safety floor so a bad config can never produce a degenerate
    near-zero call."""

    buckets_min: list[int]
    weights: list[float]
    jitter_pct: float
    min_duration_s: float

    def __post_init__(self) -> None:
        if not self.buckets_min:
            raise ValueError("duration needs at least one bucket")
        if any(b <= 0 for b in self.buckets_min):
            raise ValueError(f"duration buckets_min must all be positive, got {self.buckets_min}")
        _check_weights("duration", self.weights, self.buckets_min)
        if not (0.0 <= self.jitter_pct < 1.0):
            raise ValueError(f"duration jitter_pct must be in [0, 1), got {self.jitter_pct}")
        if self.min_duration_s <= 0:
            raise ValueError(f"duration min_duration_s must be positive, got {self.min_duration_s}")
        smallest = min(self.buckets_min) * 60.0 * (1.0 - self.jitter_pct)
        if smallest < self.min_duration_s:
            raise ValueError(
                f"duration jitter could drive a call to {smallest:.1f}s, below the "
                f"{self.min_duration_s}s floor — shrink jitter_pct or raise the smallest bucket"
            )

    def range_for(self, bucket_min: int) -> tuple[float, float]:
        """The ``generate_timing`` duration range for one bucket: bucket ± jitter, in seconds."""
        center = bucket_min * 60.0
        return (round(center * (1.0 - self.jitter_pct), 3),
                round(center * (1.0 + self.jitter_pct), 3))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DurationPolicy":
        return cls(
            buckets_min=[int(b) for b in d["buckets_min"]],
            weights=[float(w) for w in d["weights"]],
            jitter_pct=float(d["jitter_pct"]),
            min_duration_s=float(d["min_duration_s"]),
        )


@dataclass
class PartySizePolicy:
    """The class-balance axis: how many participants a call has, and each size's share.

    ``sizes`` are participant counts (e.g. [2, 3]); ``weights`` (same length, summing to
    1.0) set each size's share of the batch — i.e. the label distribution."""

    sizes: list[int]
    weights: list[float]

    def __post_init__(self) -> None:
        if not self.sizes:
            raise ValueError("party_size needs at least one size")
        if any(s < 1 for s in self.sizes):
            raise ValueError(f"party_size sizes must all be >= 1, got {self.sizes}")
        _check_weights("party_size", self.weights, self.sizes)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PartySizePolicy":
        return cls(
            sizes=[int(s) for s in d["sizes"]],
            weights=[float(w) for w in d["weights"]],
        )


@dataclass
class SessionPlan:
    """The resolved recipe for one session, ready to become a ``SessionConfig``.

    Pure data: ``participant_ips`` (the host + joiners that join Zoom, already chosen),
    which one is ``host_ip``, the ``duration_range_s`` (bucket ± jitter) and the
    ``duration_bucket_min`` it came from (recorded in the manifest), and the per-session
    ``seeds`` that make the concrete timing and turn schedule reproducible."""

    duration_bucket_min: int
    duration_range_s: tuple[float, float]
    participant_ips: list[str]
    host_ip: str
    seeds: Seeds


@dataclass
class GenerationPlan:
    """The whole generation policy: the two weighted axes. Front door: :meth:`build`."""

    duration: DurationPolicy
    party_size: PartySizePolicy

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GenerationPlan":
        return cls(
            duration=DurationPolicy.from_dict(d["duration"]),
            party_size=PartySizePolicy.from_dict(d["party_size"]),
        )

    def build(self, count: int, seed: int, client_ips: list[str]) -> list[SessionPlan]:
        """Plan ``count`` sessions from the master ``seed`` over the available ``client_ips``.

        Builds an exact-quota, shuffled schedule for each axis, then for each session
        draws which subnets participate (a random subset of the size the party-size axis
        chose) and which of them hosts. Fully deterministic for a given
        ``(self, count, seed, client_ips)``.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        largest_party = max(self.party_size.sizes)
        if len(client_ips) < largest_party:
            raise ValueError(
                f"need at least {largest_party} client IPs for the largest party size, "
                f"got {len(client_ips)}: {client_ips}"
            )
        if len(set(client_ips)) != len(client_ips):
            raise ValueError(f"client_ips must be distinct, got {client_ips}")

        rng = random.Random(seed)
        duration_schedule = _balanced_schedule(
            self.duration.buckets_min, self.duration.weights, count, rng)
        party_schedule = _balanced_schedule(
            self.party_size.sizes, self.party_size.weights, count, rng)

        plans: list[SessionPlan] = []
        for bucket, party in zip(duration_schedule, party_schedule):
            participants = rng.sample(client_ips, party)
            host = rng.choice(participants)
            seeds = Seeds(turns=rng.randrange(_SEED_SPACE),
                          timing=rng.randrange(_SEED_SPACE))
            plans.append(SessionPlan(
                duration_bucket_min=bucket,
                duration_range_s=self.duration.range_for(bucket),
                participant_ips=participants,
                host_ip=host,
                seeds=seeds,
            ))
        return plans


def load_generation_plan(path: str) -> GenerationPlan:
    """Read and validate the generation policy from a local JSON file (VM4 only).

    Unlike ``config/noise.json`` (shared via S3 because VM5 also reads it), this policy is
    read only by the bulk generator on VM4, so it stays a plain repo file."""
    with open(path, encoding="utf-8") as f:
        return GenerationPlan.from_dict(json.load(f))


def _balanced_schedule(values: list[Any], weights: list[float], count: int,
                       rng: random.Random) -> list[Any]:
    """An exact-quota, shuffled schedule of ``count`` picks from ``values``.

    Each value gets ``round(weight * count)`` slots via largest-remainder rounding (the
    leftover slots go to the largest fractional parts), so the counts sum to exactly
    ``count`` and match the weights as closely as integer counts allow. The order is then
    shuffled with ``rng``."""
    raw = [w * count for w in weights]
    counts = [int(math.floor(x)) for x in raw]
    # Largest-remainder order. Python's sort is stable even under reverse=True, so equal
    # remainders already keep ascending index order — no index tiebreak needed.
    order = sorted(range(len(values)), key=lambda i: raw[i] - counts[i], reverse=True)
    # Hand out the leftover slots. ``diff`` is normally in [0, len): the fractional parts
    # that floor() dropped. A tolerated weight sum a hair above 1.0 can over-allocate
    # (diff < 0); claw those back off the smallest remainders so the counts always sum to
    # exactly count rather than silently producing a longer schedule.
    diff = count - sum(counts)
    for k in range(abs(diff)):
        idx = order[k] if diff > 0 else order[-1 - k]
        counts[idx] += 1 if diff > 0 else -1

    schedule: list[Any] = []
    for value, c in zip(values, counts):
        schedule.extend([value] * c)
    assert len(schedule) == count, f"balanced schedule length {len(schedule)} != count {count}"
    rng.shuffle(schedule)
    return schedule


def _check_weights(name: str, weights: list[float], paired: list[Any]) -> None:
    """Validate a weight array: same length as its menu, non-negative, summing to ~1.0."""
    if len(weights) != len(paired):
        raise ValueError(
            f"{name} weights ({len(weights)}) must match the number of entries "
            f"({len(paired)})")
    if any(w < 0 for w in weights):
        raise ValueError(f"{name} weights must all be >= 0, got {weights}")
    total = math.fsum(weights)
    if abs(total - 1.0) > _WEIGHT_SUM_TOL:
        raise ValueError(f"{name} weights must sum to 1.0, got {total}")
