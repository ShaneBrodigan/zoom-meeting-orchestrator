"""Report a client's session events to S3.

Each client records timestamped events into its own ``heartbeats/{ip}.json``; VM4
reads them all after the call to fill the manifest's ``joins_leaves`` (only ``joined``
and ``left`` feed the timeline today, but ``launched``/``failed`` are kept too as raw
facts). The division of labour follows the schema's notes: the **agent** records
``launched`` (it forked the child) and ``failed`` (it saw a non-zero exit); the **bot**
records ``joined`` and ``left``.

Both of those run in *different processes* but write the *same* IP's file, so a writer
that simply overwrote would clobber the other. To avoid that, each event is appended by
read-modify-write: load whatever is already in the file, add the new event, write the
whole list back. The writes are naturally sequential (the agent writes ``launched``
before forking, the bot writes ``joined``/``left`` while it runs, the agent writes
``failed`` only after the child exits), so there is no concurrent writer to race with —
this just stops the two processes from overwriting each other's events.

The clock is injected so the recorder is testable without real time; on a VM the default
reads the chrony-aligned wall clock, which is what makes timestamps from different VMs
directly comparable.
"""

from __future__ import annotations

import time
from typing import Callable

from common.s3 import SessionStore
from common.schema import (
    EVENT_FAILED,
    EVENT_JOINED,
    EVENT_LAUNCHED,
    EVENT_LEFT,
    HeartbeatEvent,
)


class HeartbeatRecorder:
    """Append one client's events to ``heartbeats/{ip}.json``, one verb per event."""

    def __init__(self, store: SessionStore, session_id: str, ip: str, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._session_id = session_id
        self._ip = ip
        self._clock = clock

    def launched(self) -> HeartbeatEvent:
        """The agent forked the child for this session."""
        return self._record(EVENT_LAUNCHED)

    def joined(self) -> HeartbeatEvent:
        """The bot is in the meeting (the start of this client's membership window)."""
        return self._record(EVENT_JOINED)

    def left(self) -> HeartbeatEvent:
        """The bot left the meeting (the end of its membership window)."""
        return self._record(EVENT_LEFT)

    def failed(self) -> HeartbeatEvent:
        """The agent observed a non-zero child exit for this session."""
        return self._record(EVENT_FAILED)

    def _record(self, event: str) -> HeartbeatEvent:
        ev = HeartbeatEvent(event=event, ip=self._ip, ts=self._clock())
        events = self._store.read_heartbeats_for_ip(self._session_id, self._ip)
        events.append(ev)
        self._store.write_heartbeats(self._session_id, self._ip, events)
        return ev