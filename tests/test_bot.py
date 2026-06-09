"""Checkpoint tests for the bot's pure logic (client/bot.py).

The SDK glue (the ``Bot`` class) can only be exercised against the real Zoom C++ SDK on a
VM, so it is not tested here. What *is* pure — the turn-gating decision, the host-vs-joiner
zak rule, and the display-name default — is unit-tested directly. These import fine on a
machine without the SDK because client/bot.py imports zoom_meeting_sdk lazily.

Run with:  pytest tests/test_bot.py
"""

from common import schema
from common.schema import Meeting, Turns, TurnWindow
from client.bot import BotConfig, is_my_turn, speaker_at, zak_for


def make_turns():
    # 10.0.1.119 speaks [0,5); silence gap [5,6); 10.0.2.67 speaks [6,10).
    return Turns(seed=1, windows=[
        TurnWindow(0.0, 5.0, "10.0.1.119"),
        TurnWindow(6.0, 10.0, "10.0.2.67"),
    ])


# --- turn gating ----------------------------------------------------------- #

def test_speaker_at_inside_windows():
    turns = make_turns()
    assert speaker_at(turns, 0.0) == "10.0.1.119"
    assert speaker_at(turns, 4.999) == "10.0.1.119"
    assert speaker_at(turns, 6.0) == "10.0.2.67"


def test_speaker_at_in_gap_and_after_end_is_none():
    turns = make_turns()
    assert speaker_at(turns, 5.5) is None    # the silent gap between turns
    assert speaker_at(turns, 100.0) is None  # past the schedule


def test_window_is_half_open():
    # The end of a window belongs to no one (half-open [t0, t1)), so turns never overlap.
    turns = make_turns()
    assert speaker_at(turns, 5.0) is None


def test_is_my_turn_only_for_the_active_speaker():
    turns = make_turns()
    assert is_my_turn(turns, "10.0.1.119", 1.0) is True
    assert is_my_turn(turns, "10.0.2.67", 1.0) is False
    assert is_my_turn(turns, "10.0.1.119", 7.0) is False
    assert is_my_turn(turns, "10.0.2.67", 7.0) is True


# --- zak rule -------------------------------------------------------------- #

def test_host_joins_with_zak_joiner_without():
    meeting = Meeting(id="1", pwd="p", zak="ZAK")
    assert zak_for(schema.ROLE_HOST, meeting) == "ZAK"
    assert zak_for(schema.ROLE_JOINER, meeting) == ""


# --- config ---------------------------------------------------------------- #

def test_display_name_defaults_to_bot_ip():
    cfg = BotConfig(session_id="s", meeting=Meeting("1", "p", "z"), my_ip="10.0.2.67",
                    zoom_role=schema.ROLE_JOINER, turns=make_turns(),
                    anchor_epoch=1000.0, audio_path="/tmp/a.pcm")
    assert cfg.name() == "bot-10.0.2.67"
    cfg.display_name = "custom"
    assert cfg.name() == "custom"