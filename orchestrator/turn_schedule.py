"""Generate a seeded "who speaks when" schedule for one session.

Real conversation is half-duplex: one person talks while the others listen, with
short silences in between. A listening client sends almost nothing (Opus VAD/DTX
thins its packets), which is exactly the hard, realistic signal the dataset wants.
So this produces non-overlapping speaking windows with gaps of silence between them,
rather than two always-on streams.

Everything is driven by ``seed``: the same seed and inputs always yield the same
schedule, so a session is reproducible from the manifest without storing the windows
elsewhere. This is the "simple default" model (REFACTOR_DESIGN.md decision 7); it can
be made more realistic later without changing the ``Turns`` shape it returns.
"""

from __future__ import annotations

import random

from common.schema import Turns, TurnWindow

# Default conversation knobs, in seconds. Loosely modelled on ordinary speech: turns
# of a few seconds to ~12s, separated by short gaps. Tunable per call.
DEFAULT_MIN_TURN_S = 2.0
DEFAULT_MAX_TURN_S = 12.0
DEFAULT_MIN_GAP_S = 0.3
DEFAULT_MAX_GAP_S = 2.0


def generate_turns(
    seed: int,
    speakers: list[str],
    duration_s: float,
    *,
    min_turn_s: float = DEFAULT_MIN_TURN_S,
    max_turn_s: float = DEFAULT_MAX_TURN_S,
    min_gap_s: float = DEFAULT_MIN_GAP_S,
    max_gap_s: float = DEFAULT_MAX_GAP_S,
) -> Turns:
    """Build a half-duplex speaking schedule covering ``[0, duration_s)``.

    ``speakers`` are the client IPs that actually talk (the host + joiners; not noise
    or ``none`` VMs). Consecutive turns never go to the same speaker when more than one
    is present. Windows never overlap and the final window is clipped to ``duration_s``.
    Returns a ``Turns`` carrying the ``seed`` so the result is reproducible.
    """
    if not speakers:
        raise ValueError("need at least one speaker to build a turn schedule")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    if not (0 < min_turn_s <= max_turn_s):
        raise ValueError("require 0 < min_turn_s <= max_turn_s")
    if not (0 <= min_gap_s <= max_gap_s):
        raise ValueError("require 0 <= min_gap_s <= max_gap_s")

    rng = random.Random(seed)
    windows: list[TurnWindow] = []
    t = 0.0
    previous: str | None = None

    while t < duration_s:
        speaker = _next_speaker(rng, speakers, previous)
        turn_len = rng.uniform(min_turn_s, max_turn_s)
        t1 = min(t + turn_len, duration_s)
        windows.append(TurnWindow(t0=round(t, 3), t1=round(t1, 3), speaker=speaker))
        previous = speaker

        gap = rng.uniform(min_gap_s, max_gap_s)
        t = t1 + gap

    return Turns(seed=seed, windows=windows)


def _next_speaker(rng: random.Random, speakers: list[str], previous: str | None) -> str:
    """Pick a speaker, avoiding an immediate repeat when more than one is available."""
    if len(speakers) == 1:
        return speakers[0]
    choices = [s for s in speakers if s != previous]
    return rng.choice(choices)