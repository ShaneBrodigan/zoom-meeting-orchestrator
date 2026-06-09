"""The long-running container front door on each subnet client (VM1/2/3/5).

One container per VM runs :meth:`Agent.run_forever`. It polls S3 for session specs,
matches its **own private IP** against the roster (network position is the single source
of truth for role — REFACTOR_DESIGN.md decision 3), and forks a :mod:`client.bot` child
per session it belongs to. The child joins Zoom and dies with the call, giving each
session fresh ALSA/Pulse state; the parent just keeps polling (decision 9).

Two deliberate behaviours:

* **Only new sessions are acted on.** On start-up the agent *primes* its seen-set with
  every session already in the bucket, so a fresh container never tries to re-join the
  historical meetings sitting in ``sessions/`` — it acts only on specs that appear after
  it came up.
* **Noise is not spec-triggered.** A roster entry with ``zoom_role: none`` (e.g. VM5) is
  ignored here. Background noise runs independently on its own schedule so it also fills
  the pre-roll/post-roll/gaps (decision 10); the agent never starts it from the spec. So
  in this phase the agent only ever launches the bot, for ``host``/``joiner`` entries.

The two things that touch the outside world — *how do I learn my own IP* and *how do I
launch a child* — are injected, so the poll/match/dedupe logic is exercised in tests with
fakes and no real forking, sockets, or SDK.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

from common.s3 import AUDIO_SOURCE_KEY, SessionStore
from common.schema import ROLE_NONE, RosterEntry, Spec
from client.bot import BotConfig, run_bot
from client.heartbeat import HeartbeatRecorder

DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_AUDIO_PATH = "/tmp/librispeech_audio.pcm"

# Optional override for this client's private IP. The roster is keyed on the VM's real
# subnet IP (e.g. 10.0.1.119); auto-detection only returns that when the container shares
# the host network (run with --network host). This env var is the safety valve when it
# can't — set AGENT_IP to the VM's private IP and the agent uses it verbatim.
ENV_AGENT_IP = "AGENT_IP"

# A launcher starts the work for one session this client belongs to. Injectable so tests
# can record calls instead of forking; the default forks a real bot child.
Launcher = Callable[[Spec, RosterEntry, float, str], object]


class Agent:
    """Polls S3 and forks a bot per session this client is rostered into."""

    def __init__(self, store: SessionStore, my_ip: str, *,
                 launch: Launcher | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
                 audio_path: str = DEFAULT_AUDIO_PATH) -> None:
        self._store = store
        self._ip = my_ip
        self._launch = launch or (lambda spec, entry, anchor, audio:
                                  _fork_bot(store, spec, entry, anchor, audio))
        self._sleep = sleep
        self._poll_interval_s = poll_interval_s
        self._audio_path = audio_path
        self._handled: set[str] = set()
        self._audio_ready = False

    @classmethod
    def from_env(cls, *, launch: Launcher | None = None) -> "Agent":
        """Build for a VM: IAM-role S3 store and this host's private IP.

        The IP comes from ``AGENT_IP`` if set (the override for when the container can't
        see the host network), otherwise it is auto-detected."""
        ip = os.environ.get(ENV_AGENT_IP) or detect_private_ip()
        return cls(SessionStore(), ip, launch=launch)

    # --- front door -------------------------------------------------------- #

    def run_forever(self) -> None:
        """Prime past sessions, then poll for new ones forever."""
        self.prime()
        while True:
            self.poll_once()
            self._sleep(self._poll_interval_s)

    def prime(self) -> None:
        """Mark every session already present as handled, so only later ones are acted on."""
        self._handled |= set(self._store.list_session_ids())

    def poll_once(self) -> None:
        """Handle every session not yet seen. Marks a session handled before acting so a
        transient error can't make it re-launch the same meeting on the next tick."""
        for session_id in self._store.list_session_ids():
            if session_id in self._handled:
                continue
            self._handled.add(session_id)
            self._handle(session_id)

    # --- internals --------------------------------------------------------- #

    def _handle(self, session_id: str) -> None:
        spec, anchor = self._store.read_spec_with_anchor(session_id)
        entry = spec.entry_for_ip(self._ip)
        if entry is None or entry.zoom_role == ROLE_NONE:
            return  # not a meeting participant here (noise runs independently of the spec)
        self._launch(spec, entry, anchor, self._ensure_audio())

    def _ensure_audio(self) -> str:
        """Fetch the shared LibriSpeech source once (it is not baked into the image)."""
        if not self._audio_ready:
            self._store.download_audio_source(self._audio_path, key=AUDIO_SOURCE_KEY)
            self._audio_ready = True
        return self._audio_path


def bot_config_from_spec(spec: Spec, entry: RosterEntry, anchor: float,
                         audio_path: str) -> BotConfig:
    """Project the spec + this client's roster entry into the bot's flat config."""
    return BotConfig(
        session_id=spec.session_id,
        meeting=spec.meeting,
        my_ip=entry.ip,
        zoom_role=entry.zoom_role,
        turns=spec.turns,
        anchor_epoch=anchor,
        audio_path=audio_path,
        join_delay_s=spec.timing.join_delay_s.get(entry.ip, 0.0),
    )


def _fork_bot(store: SessionStore, spec: Spec, entry: RosterEntry, anchor: float,
              audio_path: str):
    """Default launcher: record ``launched`` and fork the bot child for this session."""
    import multiprocessing

    HeartbeatRecorder(store, spec.session_id, entry.ip).launched()
    config = bot_config_from_spec(spec, entry, anchor, audio_path)
    proc = multiprocessing.Process(target=run_bot, args=(config,))
    proc.start()
    return proc


def detect_private_ip() -> str:
    """This host's primary private IP, via the egress-interface trick (sends no packets)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # picks the outbound interface; no traffic sent
        return sock.getsockname()[0]
    finally:
        sock.close()