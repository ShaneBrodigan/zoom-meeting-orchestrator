"""Start and stop the tshark packet capture for one session (VM4 only).

VM4 is the capture host. This module wraps the single ``tshark`` invocation that
records a session to one ``capture.pcap``, applying the exact pre-NAT filter from
REFACTOR_DESIGN.md decision 2: keep the four client subnets (real client source
IPs → per-participant attribution) and drop SSH so VM4's own control traffic never
lands in the dataset. The post-NAT ``10.0.0.7`` twins are excluded for free,
because that IP is not in any client subnet.

The one invariant this enforces: :meth:`PacketCapture.start` returns only once
tshark is *actually capturing* (it waits for tshark's "Capturing on" readiness
line on stderr). The orchestrator relies on that — it starts the capture before it
publishes the spec, and clients cannot see (or join) the session until the spec
exists, so every join is captured (decision 5).

The process launcher is injectable and the heavy bits are lazily reachable,
mirroring how ``common/s3.py`` injects boto3: on VM4 the default launches real
tshark; tests pass a small fake process so the command-building and the
start/stop lifecycle run without tshark, root, or an ``ens5`` to listen on. The
two wall-clock timestamps it records (``t_start`` / ``t_stop``) are what the
``Capture`` schema needs to bound the capture window for the manifest.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

DEFAULT_INTERFACE = "ens5"

# The client subnets whose traffic we keep (REFACTOR_DESIGN.md decision 2). Order
# matches the documented filter so the generated expression is byte-for-byte that.
CLIENT_SUBNETS = ("10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24", "10.0.4.0/24")

# Excluded so VM4's SSH control traffic never enters a labeled capture.
_EXCLUDE_EXPR = "not tcp port 22"

# Per-packet capture length (tshark ``-s``): keep only the first N bytes of each packet.
# Noise packets are full-MTU (~1400 B), so truncating them here is what keeps a 30-minute
# noise-heavy pcap from reaching gigabytes. 256 B comfortably preserves every L2-L4 header
# (so the flow 5-tuples the labeler needs are intact) plus the first payload bytes ET-BERT
# tokenizes; it only drops the deep-payload tail neither the labeler nor ET-BERT reads.
DEFAULT_SNAPLEN = 256

# tshark prints this to stderr once the capture is live and the pcap is open.
_READY_MARKER = "Capturing on"


class CaptureError(RuntimeError):
    """tshark could not be started or stopped cleanly."""


# A launcher takes the argv and returns a process handle exposing the small surface
# this module uses: ``stderr.readline()``, ``poll()``, ``terminate()``, ``wait()``.
Popen = Callable[[list[str]], Any]


class PacketCapture:
    """One session's tshark capture: build the command, start it, stop it.

    Use it directly (``cap.start(); ...; cap.stop()``) or as a context manager
    (``with PacketCapture(path) as cap: ...``) to guarantee the capture is stopped
    even if the session body raises."""

    def __init__(self, pcap_path: str, *, interface: str = DEFAULT_INTERFACE,
                 subnets: tuple[str, ...] = CLIENT_SUBNETS,
                 snaplen: int = DEFAULT_SNAPLEN,
                 popen: Popen | None = None,
                 clock: Callable[[], float] = time.time,
                 ready_timeout_s: float = 15.0,
                 stop_timeout_s: float = 10.0) -> None:
        self.pcap_path = pcap_path
        self.interface = interface
        self.subnets = subnets
        self.snaplen = snaplen
        self._popen = popen if popen is not None else _default_popen
        self._clock = clock
        self._ready_timeout_s = ready_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._proc: Any = None
        self._t_start: float | None = None
        self._t_stop: float | None = None

    # --- the filter / command (the single source of truth for what we capture) --- #

    @property
    def capture_filter(self) -> str:
        """The BPF filter: keep the client subnets, drop SSH (decision 2)."""
        nets = " or ".join(f"net {s}" for s in self.subnets)
        return f"({nets}) and {_EXCLUDE_EXPR}"

    def build_argv(self) -> list[str]:
        return [
            "tshark",
            "-i", self.interface,
            "-n",                       # no name resolution: tshark adds no lookup traffic of its own
            "-f", self.capture_filter,
            "-s", str(self.snaplen),    # truncate each packet: keeps headers + first payload bytes
            "-w", self.pcap_path,
        ]

    # --- front door -------------------------------------------------------- #

    def start(self) -> float:
        """Launch tshark and return ``t_start``, only once it is really capturing."""
        if self._proc is not None:
            raise CaptureError("capture already started")
        self._proc = self._popen(self.build_argv())
        self._await_ready()
        self._t_start = self._clock()
        return self._t_start

    def stop(self) -> float:
        """Stop tshark cleanly and return ``t_stop`` (the end of the capture window)."""
        if self._proc is None:
            raise CaptureError("capture not started")
        if self._t_stop is not None:
            return self._t_stop
        self._proc.terminate()
        try:
            self._proc.wait(timeout=self._stop_timeout_s)
        except Exception as err:  # includes subprocess.TimeoutExpired
            raise CaptureError(f"tshark did not stop within {self._stop_timeout_s}s") from err
        self._t_stop = self._clock()
        return self._t_stop

    @property
    def t_start(self) -> float | None:
        return self._t_start

    @property
    def t_stop(self) -> float | None:
        return self._t_stop

    def __enter__(self) -> "PacketCapture":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._proc is not None and self._t_stop is None:
            self.stop()

    # --- internals --------------------------------------------------------- #

    def _await_ready(self) -> None:
        """Block until tshark reports it is capturing, or it exits / times out."""
        deadline = self._clock() + self._ready_timeout_s
        stderr = self._proc.stderr
        while True:
            line = stderr.readline() if stderr is not None else ""
            if line:
                text = line.decode(errors="replace") if isinstance(line, bytes) else line
                if _READY_MARKER in text:
                    return
            elif self._proc.poll() is not None:
                raise CaptureError(
                    f"tshark exited before capturing (rc={self._proc.returncode}); "
                    f"check the interface, filter, and capture permissions"
                )
            if self._clock() >= deadline:
                raise CaptureError("timed out waiting for tshark to start capturing")


def _default_popen(argv: list[str]) -> Any:
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)