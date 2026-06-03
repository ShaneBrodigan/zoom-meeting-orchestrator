"""Pick the seeded, randomized timing for one session.

Timing is deliberately randomized so the model can't learn the harness's fixed cadence
as a fingerprint (REFACTOR_DESIGN.md decision 4). But the randomness is bounded by two
rules from REFACTOR_DESIGN.md section 6:

* **Join offsets must be much smaller than the call duration.** Every client has to be
  on the call well before it ends, so there is a long, genuine N-party overlap window
  rather than the call ending mid-join. Enforced by capping the join window to a small
  fraction of the duration.
* **Pre/post-roll are capped**, so the capture doesn't fill with dead air before the
  first join or after the meeting ends.

Driven entirely by ``seed`` (with a fixed draw order), so a session's timing is
reproducible from the manifest.
"""

from __future__ import annotations

import random

from common.schema import Timing

# Default bounds, in seconds. All tunable per call by the orchestrator.
DEFAULT_PREROLL_RANGE_S = (2.0, 8.0)
DEFAULT_DURATION_RANGE_S = (60.0, 180.0)
DEFAULT_POSTROLL_RANGE_S = (2.0, 8.0)

# The join window is capped to this fraction of the chosen duration: the last client
# joins no later than fraction * duration in, guaranteeing a long N-party overlap.
DEFAULT_JOIN_WINDOW_FRACTION = 0.25
# Absolute ceiling on the join window regardless of duration, so long calls don't get
# an absurdly long ramp-up.
DEFAULT_MAX_JOIN_DELAY_S = 10.0


def generate_timing(
    seed: int,
    client_ips: list[str],
    *,
    preroll_range_s: tuple[float, float] = DEFAULT_PREROLL_RANGE_S,
    duration_range_s: tuple[float, float] = DEFAULT_DURATION_RANGE_S,
    postroll_range_s: tuple[float, float] = DEFAULT_POSTROLL_RANGE_S,
    join_window_fraction: float = DEFAULT_JOIN_WINDOW_FRACTION,
    max_join_delay_s: float = DEFAULT_MAX_JOIN_DELAY_S,
) -> Timing:
    """Build the timing for a session.

    ``client_ips`` are the IPs that join the meeting (host + joiners; not noise/``none``
    VMs). Each gets its own join offset, in seconds from the moment capture starts.
    The draw order is fixed (preroll, duration, postroll, then join delays in
    ``client_ips`` order), so the result is reproducible for a given seed and input.
    """
    if not client_ips:
        raise ValueError("need at least one joining client to build timing")
    for name, rng_bounds in (
        ("preroll_range_s", preroll_range_s),
        ("duration_range_s", duration_range_s),
        ("postroll_range_s", postroll_range_s),
    ):
        lo, hi = rng_bounds
        if not (0 <= lo <= hi):
            raise ValueError(f"{name} must satisfy 0 <= lo <= hi, got {rng_bounds}")
    if not (0 < join_window_fraction < 1):
        raise ValueError(f"join_window_fraction must be in (0, 1), got {join_window_fraction}")
    if max_join_delay_s < 0:
        raise ValueError(f"max_join_delay_s must be >= 0, got {max_join_delay_s}")

    rng = random.Random(seed)

    preroll_s = rng.uniform(*preroll_range_s)
    duration_s = rng.uniform(*duration_range_s)
    postroll_s = rng.uniform(*postroll_range_s)

    # Cap the join window so every client is on the call well before it ends.
    join_window_s = min(max_join_delay_s, duration_s * join_window_fraction)
    join_delay_s = {ip: round(rng.uniform(0.0, join_window_s), 3) for ip in client_ips}

    return Timing(
        preroll_s=round(preroll_s, 3),
        duration_s=round(duration_s, 3),
        postroll_s=round(postroll_s, 3),
        join_delay_s=join_delay_s,
    )
