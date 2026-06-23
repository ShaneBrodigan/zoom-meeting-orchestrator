"""Generate a seeded "who speaks when" schedule for one session.

Real conversation is mostly half-duplex: one person talks while the others listen,
with short silences in between. A listening client sends almost nothing (Opus VAD/DTX
thins its packets), which is exactly the hard, realistic signal the dataset wants. So
the backbone is one speaker at a time with short gaps, rather than two always-on streams.

On top of that backbone, three realistic conversation events are injected so the
timing isn't artificially tidy (a fixed metronome the model could fingerprint):
  * a **long "thinking" pause** (4-6s) sometimes replaces the short gap between turns;
  * a **brief overlap** where the next speaker starts before the current one stops, so
    they talk over each other for a moment before one drops out;
  * a **backchannel** where a different speaker drops a short "yeah/agreed" burst inside
    someone's turn without taking it over.
These produce *overlapping* windows (two speakers active at once), so consumers must
treat the schedule as "any window covering this moment", not "the one speaker".

Note the bots play from the shared LibriSpeech audiobook, so a backchannel is a short
snippet of that audio, not the literal word "yeah" — what matters for an encrypted-
traffic dataset is the packet shape of a brief second-speaker burst, not the words.

Everything is driven by ``seed``: the same seed and inputs always yield the same
schedule, so a session is reproducible from the manifest without storing the windows
elsewhere (REFACTOR_DESIGN.md decision 7). The ``Turns`` shape is unchanged.
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

# Realistic conversation events, baked in (not per-session knobs). Probabilities are
# per-transition (long pause, overlap) or per-turn (backchannel); ranges are seconds.
# Overlap and backchannel need a second speaker, so they only fire on multi-party calls.
LONG_PAUSE_PROB = 0.10        # an "everyone's thinking" silence instead of a short gap
LONG_PAUSE_MIN_S = 4.0
LONG_PAUSE_MAX_S = 6.0

OVERLAP_PROB = 0.05           # next speaker starts before this one stops (brief double-talk)
OVERLAP_MIN_S = 0.3
OVERLAP_MAX_S = 1.0

BACKCHANNEL_PROB = 0.10       # a short "yeah/agreed" by someone else, mid-turn
BACKCHANNEL_MIN_S = 0.4
BACKCHANNEL_MAX_S = 1.2


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
    """Build a speaking schedule covering ``[0, duration_s)``.

    ``speakers`` are the client IPs that actually talk (the host + joiners; not noise
    or ``none`` VMs). The backbone is one speaker at a time — consecutive turns never go
    to the same speaker when more than one is present — but realistic events (long
    pauses, brief overlaps, backchannels; see the module docstring) are injected on top,
    so windows **can** overlap. The final window is clipped to ``duration_s``. Returns a
    ``Turns`` carrying the ``seed`` so the result is reproducible.
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
    multi = len(speakers) > 1
    windows: list[TurnWindow] = []
    t = 0.0
    previous: str | None = None

    while t < duration_s:
        speaker = _next_speaker(rng, speakers, previous)
        turn_len = rng.uniform(min_turn_s, max_turn_s)
        t1 = min(t + turn_len, duration_s)
        windows.append(TurnWindow(t0=round(t, 3), t1=round(t1, 3), speaker=speaker))

        # Backchannel: a different speaker drops a short interjection inside this turn
        # without ending it. Needs a second person and a turn long enough to hold it.
        if multi and rng.random() < BACKCHANNEL_PROB:
            bc_len = rng.uniform(BACKCHANNEL_MIN_S, BACKCHANNEL_MAX_S)
            if (t1 - t) >= bc_len:
                bc_speaker = _next_speaker(rng, speakers, speaker)
                bc_start = rng.uniform(t, t1 - bc_len)
                windows.append(TurnWindow(t0=round(bc_start, 3),
                                          t1=round(bc_start + bc_len, 3),
                                          speaker=bc_speaker))

        previous = speaker

        # Spacing to the next turn: a brief overlap (next speaker starts before this one
        # stops), a long thinking pause, or an ordinary short gap. The overlap band is
        # always reserved so single-speaker calls map it to a normal gap, not a pause.
        roll = rng.random()
        if multi and roll < OVERLAP_PROB:
            t = t1 - rng.uniform(OVERLAP_MIN_S, OVERLAP_MAX_S)
        elif OVERLAP_PROB <= roll < OVERLAP_PROB + LONG_PAUSE_PROB:
            t = t1 + rng.uniform(LONG_PAUSE_MIN_S, LONG_PAUSE_MAX_S)
        else:
            t = t1 + rng.uniform(min_gap_s, max_gap_s)

    # Backchannels are appended out of order; sort so the schedule reads chronologically.
    windows.sort(key=lambda w: (w.t0, w.t1))
    return Turns(seed=seed, windows=windows)


def _next_speaker(rng: random.Random, speakers: list[str], previous: str | None) -> str:
    """Pick a speaker, avoiding an immediate repeat when more than one is available."""
    if len(speakers) == 1:
        return speakers[0]
    choices = [s for s in speakers if s != previous]
    return rng.choice(choices)