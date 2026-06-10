"""VM5's standalone background-noise generator (REFACTOR_DESIGN.md decision 10).

A program that runs **forever**, completely independent of any Zoom session. In a loop
it fires one short burst of iperf traffic at a dedicated internet server, then idles a
random gap, then repeats. Because it knows nothing about call timing, its traffic
blankets the recorded pre-roll, the mid-call gaps, and the post-roll — denying a model
the cheap "any traffic at all = a call" shortcut.

iperf is the muscle (it just moves bytes for a fixed time); this module is the brain
that decides *when* and *how hard*. iperf has no scheduler of its own — it runs one
transfer then exits — so the burst/idle loop is ours. Every per-burst knob (length,
rate, TCP/UDP, port, upload-vs-download) is drawn from the configured ranges with a
seeded RNG, so the noise is reproducible (good for the thesis) yet varied (no single
learnable signature). It is deliberately *not* the agent and *not* spec-triggered: its
own front door, started once at provisioning and left running (``--restart=always``).

The two edges that touch the outside world — **the clock** (sleeping the gaps) and
**running an iperf command** — are injected, exactly like ``agent.py`` /
``session_orchestrator.py`` inject their edges. So the whole scheduling brain is unit
tested with fakes: no real iperf, no real sleeping, no network.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from common.noise_config import PROTO_UDP, NoiseConfig
from common.s3 import SessionStore

# The two injected edges. ``RunIperf`` takes a ready argv and runs one transfer
# (blocking until it finishes); ``Sleep`` idles the gap between bursts.
RunIperf = Callable[[list[str]], None]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class IperfBurst:
    """One drawn iperf transfer: its length, rate, transport, port, and direction."""
    duration_s: int        # whole seconds (iperf -t takes integer seconds)
    rate_mbps: float       # push rate, Mbps
    protocol: str          # "tcp" | "udp"
    port: int
    reverse: bool          # True = download (server->VM5, iperf -R); False = upload


def build_iperf_command(burst: IperfBurst, target: str) -> list[str]:
    """The iperf3 client command line for one burst (a pure function, easy to test)."""
    cmd = [
        "iperf3",
        "-c", target,
        "-p", str(burst.port),
        "-t", str(burst.duration_s),
        "-b", f"{burst.rate_mbps:g}M",
    ]
    if burst.protocol == PROTO_UDP:
        cmd.append("-u")   # default is TCP; -u switches the transport to UDP
    if burst.reverse:
        cmd.append("-R")   # reverse: the server sends, VM5 receives (a download)
    return cmd


class NoiseGenerator:
    """The seeded burst/idle loop. ``run_forever`` is the front door."""

    def __init__(self, config: NoiseConfig, *, run_iperf: RunIperf,
                 sleep: Sleep = time.sleep) -> None:
        self._config = config
        self._run_iperf = run_iperf
        self._sleep = sleep
        self._rng = random.Random(config.seed)

    @classmethod
    def from_env(cls) -> "NoiseGenerator":
        """Build for VM5: read ``config/noise.json`` via the instance-role S3, run real iperf."""
        config = SessionStore().read_noise_config()
        return cls(config, run_iperf=_run_iperf_subprocess)

    # --- front door -------------------------------------------------------- #

    def run_forever(self) -> None:
        """Fire bursts with random gaps between, forever (stopped by hand / container stop)."""
        while True:
            self.run_cycle()

    def run_cycle(self) -> None:
        """One cycle: run a drawn burst to completion, then idle a drawn gap.

        The RNG is consumed burst-then-gap, the same order every cycle, so the whole
        sequence is reproducible from the seed."""
        burst = self.draw_burst()
        self._run_iperf(build_iperf_command(burst, self._config.target))
        self._sleep(self.draw_gap())

    # --- the seeded draws (public so they can be checked directly) --------- #

    def draw_burst(self) -> IperfBurst:
        """Draw one burst's parameters from the configured ranges/sets."""
        c = self._config
        return IperfBurst(
            duration_s=self._rng.randint(c.burst_s[0], c.burst_s[1]),
            rate_mbps=round(self._rng.uniform(c.rate_mbps[0], c.rate_mbps[1]), 1),
            protocol=self._rng.choice(c.protocols),
            port=self._rng.choice(c.ports),
            reverse=self._rng.random() < c.reverse_prob,
        )

    def draw_gap(self) -> float:
        """Draw one idle gap (seconds) from the configured range."""
        return round(self._rng.uniform(self._config.gap_s[0], self._config.gap_s[1]), 1)


def _run_iperf_subprocess(argv: list[str]) -> None:
    """Run one real iperf transfer, blocking until it finishes.

    A failed transfer (server momentarily down, port busy) must not kill the loop — it
    just becomes a quieter stretch in the capture, which is fine — so a non-zero exit is
    not raised."""
    import subprocess

    subprocess.run(argv, check=False)


def main() -> None:
    NoiseGenerator.from_env().run_forever()


if __name__ == "__main__":
    main()
