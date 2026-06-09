"""Checkpoint tests for the client agent poller (client/agent.py).

Local, no AWS / SDK / forking: a real SessionStore over the in-memory FakeS3, with the
launcher injected as a recording stub. These prove the poll/match/dedupe behaviour the
agent is responsible for:

* it launches a bot for a spec whose roster names this client's IP (host or joiner),
* it ignores specs where this IP is absent or is a ``none`` (noise) entry,
* it acts on each new session exactly once, and
* priming makes a fresh agent skip the sessions already present at start-up.

Run with:  pytest tests/test_agent.py
"""

import pytest

from common import schema
from common.s3 import AUDIO_SOURCE_KEY, SessionStore
from common.schema import (
    Meeting,
    NoiseBlock,
    RosterEntry,
    Seeds,
    Spec,
    Timing,
    Turns,
    TurnWindow,
)
from client.agent import Agent

from tests.test_s3 import FakeS3


def make_spec(session_id, roster):
    return Spec(
        session_id=session_id,
        meeting=Meeting(id="123456789", pwd="pw", zak="zak"),
        participant_count=sum(1 for e in roster if e.zoom_role != schema.ROLE_NONE),
        roster=roster,
        turns=Turns(seed=1, windows=[TurnWindow(0.0, 6.0, "10.0.1.119")]),
        timing=Timing(preroll_s=2.0, duration_s=60.0, postroll_s=2.0,
                      join_delay_s={"10.0.1.119": 0.0, "10.0.2.67": 3.0}),
        seeds=Seeds(turns=1, timing=2),
    )


TWO_PARTY = [
    RosterEntry(ip="10.0.1.119", zoom_role=schema.ROLE_HOST),
    RosterEntry(ip="10.0.2.67", zoom_role=schema.ROLE_JOINER),
]


def make_agent(my_ip, tmp_path):
    store = SessionStore(bucket="test-bucket", client=FakeS3())
    store._client.objects[AUDIO_SOURCE_KEY] = b"PCMDATA"  # so _ensure_audio can download
    launched = []
    agent = Agent(store, my_ip,
                  launch=lambda spec, entry, anchor, audio:
                      launched.append((spec.session_id, entry.ip, entry.zoom_role,
                                       anchor, audio)),
                  audio_path=str(tmp_path / "audio.pcm"))
    return agent, store, launched


# --- matching -------------------------------------------------------------- #

def test_launches_for_host_ip(tmp_path):
    agent, store, launched = make_agent("10.0.1.119", tmp_path)
    store.publish_spec(make_spec("sess-1", TWO_PARTY))
    agent.poll_once()
    assert len(launched) == 1
    session_id, ip, role, anchor, audio = launched[0]
    assert (session_id, ip, role) == ("sess-1", "10.0.1.119", schema.ROLE_HOST)
    assert isinstance(anchor, float)            # the t=0 anchor was read
    assert audio.endswith("audio.pcm")


def test_launches_for_joiner_ip(tmp_path):
    agent, store, launched = make_agent("10.0.2.67", tmp_path)
    store.publish_spec(make_spec("sess-1", TWO_PARTY))
    agent.poll_once()
    assert [l[2] for l in launched] == [schema.ROLE_JOINER]


def test_ip_not_in_roster_does_nothing(tmp_path):
    agent, store, launched = make_agent("10.0.9.9", tmp_path)
    store.publish_spec(make_spec("sess-1", TWO_PARTY))
    agent.poll_once()
    assert launched == []


def test_noise_only_entry_is_not_launched(tmp_path):
    # VM5 is zoom_role 'none'; noise runs independently, not from the spec.
    agent, store, launched = make_agent("10.0.4.16", tmp_path)
    roster = TWO_PARTY + [RosterEntry(ip="10.0.4.16", zoom_role=schema.ROLE_NONE,
                                      noise=NoiseBlock(enabled=True, profile="iperf"))]
    store.publish_spec(make_spec("sess-1", roster))
    agent.poll_once()
    assert launched == []


# --- dedupe / priming ------------------------------------------------------ #

def test_each_session_launched_once_across_polls(tmp_path):
    agent, store, launched = make_agent("10.0.1.119", tmp_path)
    store.publish_spec(make_spec("sess-1", TWO_PARTY))
    agent.poll_once()
    agent.poll_once()  # same session still present; must not launch again
    assert len(launched) == 1


def test_prime_skips_sessions_present_at_startup(tmp_path):
    agent, store, launched = make_agent("10.0.1.119", tmp_path)
    store.publish_spec(make_spec("old-session", TWO_PARTY))
    agent.prime()                 # a fresh container coming up over an existing bucket
    agent.poll_once()
    assert launched == []         # the historical meeting is not re-joined

    store.publish_spec(make_spec("new-session", TWO_PARTY))
    agent.poll_once()
    assert [l[0] for l in launched] == ["new-session"]  # only the later spec is acted on


# --- audio fetch ----------------------------------------------------------- #

def test_audio_downloaded_once_across_sessions(tmp_path):
    agent, store, launched = make_agent("10.0.1.119", tmp_path)
    calls = {"n": 0}
    original = store.download_audio_source

    def spy(local_path, **kw):
        calls["n"] += 1
        original(local_path, **kw)
    store.download_audio_source = spy  # type: ignore[assignment]

    store.publish_spec(make_spec("sess-1", TWO_PARTY))
    store.publish_spec(make_spec("sess-2", TWO_PARTY))
    agent.poll_once()
    assert len(launched) == 2
    assert calls["n"] == 1  # the shared source is fetched once, then reused