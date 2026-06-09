"""Checkpoint tests for the client heartbeat recorder (client/heartbeat.py).

Local, no AWS: a real SessionStore over the in-memory FakeS3, with an injected clock so
timestamps are deterministic. The key things to prove are that each verb writes the right
event and that two writers of the *same* IP file (the agent and the bot, in real life
different processes) don't clobber each other.

Run with:  pytest tests/test_heartbeat.py
"""

from common import schema
from common.s3 import SessionStore
from client.heartbeat import HeartbeatRecorder

from tests.test_s3 import FakeS3


def make_recorder(ip="10.0.1.119", times=None):
    store = SessionStore(bucket="test-bucket", client=FakeS3())
    ticks = iter(times or range(1, 100))
    rec = HeartbeatRecorder(store, "sess-1", ip, clock=lambda: float(next(ticks)))
    return rec, store


def test_each_verb_writes_its_event():
    rec, store = make_recorder()
    rec.launched()
    rec.joined()
    rec.left()
    events = store.read_heartbeats_for_ip("sess-1", "10.0.1.119")
    assert [e.event for e in events] == [
        schema.EVENT_LAUNCHED, schema.EVENT_JOINED, schema.EVENT_LEFT,
    ]


def test_event_carries_ip_and_clock_timestamp():
    rec, store = make_recorder(ip="10.0.2.67", times=[42])
    ev = rec.joined()
    assert ev.ip == "10.0.2.67"
    assert ev.ts == 42.0
    assert ev.event == schema.EVENT_JOINED


def test_two_writers_on_same_ip_do_not_clobber():
    # The agent (launched/failed) and the bot (joined/left) write the same file from
    # different recorders; read-modify-write must preserve all four events.
    store = SessionStore(bucket="test-bucket", client=FakeS3())
    agent = HeartbeatRecorder(store, "sess-1", "10.0.1.119", clock=lambda: 1.0)
    bot = HeartbeatRecorder(store, "sess-1", "10.0.1.119", clock=lambda: 2.0)

    agent.launched()      # parent, before forking
    bot.joined()          # child, in the meeting
    bot.left()            # child, on the way out
    agent.failed()        # parent, after observing exit

    events = store.read_heartbeats_for_ip("sess-1", "10.0.1.119")
    assert {e.event for e in events} == {
        schema.EVENT_LAUNCHED, schema.EVENT_JOINED,
        schema.EVENT_LEFT, schema.EVENT_FAILED,
    }