"""VM5's standalone background-noise generator (REFACTOR_DESIGN.md decision 10).

A program that runs **forever**, completely independent of any Zoom session. In a loop
it fires one short burst of background traffic at the internet, then idles a random gap,
then repeats. Because it knows nothing about call timing, its traffic blankets the
recorded pre-roll, the mid-call gaps, and the post-roll — denying a model the cheap "any
traffic at all = a call" shortcut.

The burst/idle *loop* is the brain; the muscle is whichever **traffic profile** the loop
draws for that burst:

* **iperf** — raw throughput against the dedicated internet server (real packets, but
  synthetic *behaviour*: it just moves bytes);
* **download** — a web file pull via ``curl`` from a curated, pinned set of public URLs;
* **video** — a real-time video pull via ``ffmpeg`` against a public HLS/DASH stream.

iperf was the first profile; download and video are the second and third — the real
callers that justify splitting "what one burst does" out from the loop (decision 10 said
to keep the loop separate from the iperf call so a real-app profile could be added later,
but *not* to build a plugin layer until a 2nd profile actually existed). The loop stays;
only the per-burst command becomes pluggable. Each profile draws its own per-burst knobs
(length, rate, URL, ...) from the configured ranges with a single seeded RNG, so the whole
mixed sequence is reproducible (good for the thesis) yet varied (no single learnable
signature). It is deliberately *not* the agent and *not* spec-triggered: its own front
door, started once at provisioning and left running (``--restart=always``).

The two edges that touch the outside world — **the clock** (sleeping the gaps) and
**running a traffic command** — are injected, exactly like ``agent.py`` /
``session_orchestrator.py`` inject their edges. So the whole scheduling brain is unit
tested with fakes: no real iperf/curl/ffmpeg, no real sleeping, no network.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from common.noise_config import (
    PROFILE_DOWNLOAD,
    PROFILE_IPERF,
    PROFILE_VIDEO,
    PROTO_UDP,
    DownloadProfile,
    IperfProfile,
    NoiseConfig,
    VideoProfile,
)
from common.s3 import SessionStore

# The two injected edges. ``RunCommand`` takes a ready argv and runs one burst to
# completion (blocking); ``Sleep`` idles the gap between bursts.
RunCommand = Callable[[list[str]], None]
Sleep = Callable[[float], None]

# Mbps -> bytes/second, for tools (curl) whose rate cap is expressed in bytes/s.
_BYTES_PER_SEC_PER_MBPS = 125_000  # 1 Mbit/s = 1e6 bits / 8 = 125,000 bytes/s


# --------------------------------------------------------------------------- #
# Per-burst shapes + the pure command builders (one per profile, easy to test)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IperfBurst:
    """One drawn iperf transfer: its length, rate, transport, port, and direction."""
    duration_s: int        # whole seconds (iperf -t takes integer seconds)
    rate_mbps: float       # push rate, Mbps
    protocol: str          # "tcp" | "udp"
    port: int
    reverse: bool          # True = download (server->VM5, iperf -R); False = upload


@dataclass(frozen=True)
class DownloadBurst:
    """One drawn web download: which URL, its speed cap, and its time bound."""
    url: str
    rate_mbps: float
    max_time_s: int


@dataclass(frozen=True)
class VideoBurst:
    """One drawn video stream pull: which stream and how many seconds to play."""
    stream: str
    duration_s: int


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


def build_curl_command(burst: DownloadBurst) -> list[str]:
    """The curl command line for one download burst (pure).

    ``-s`` quiet, ``-o /dev/null`` discards the file (only the network traffic matters,
    like the bot's stripped disk I/O). ``--max-time`` bounds the burst; ``--limit-rate``
    caps the speed — expressed in bytes/second (an integer) to avoid curl's fractional
    suffix parsing.

    ``-f`` makes an HTTP error (403/404, or a host rate-limiting us with 429) move zero
    bytes cleanly instead of writing an error page; ``--retry`` rides a *brief* throttle (a
    transient 429/5xx/timeout is retried; a dead 404 is not, so a bad URL still fails fast).
    ``-w %{size_download}`` makes curl print the bytes it actually moved, so the runner can
    tell a real failure (≈0 bytes) from a *successful* long burst that curl aborts at
    ``--max-time`` having already pulled megabytes (a non-zero exit, but real traffic — the
    desired big burst). This is why the URL pool spans several hosts: load spreads so no
    single host trips its limit, and if one does, the others still carry the noise."""
    limit_bytes_per_s = int(burst.rate_mbps * _BYTES_PER_SEC_PER_MBPS)
    return [
        "curl", "-s", "-f", "-o", "/dev/null", "-w", "%{size_download}",
        "--retry", "2", "--retry-delay", "2",
        "--max-time", str(burst.max_time_s),
        "--limit-rate", str(limit_bytes_per_s),
        burst.url,
    ]


def build_ffmpeg_command(burst: VideoBurst) -> list[str]:
    """The ffmpeg command line for one video burst (pure).

    ``-re`` pulls at real-time playback pace (segment, wait, segment) like a real player;
    ``-t`` bounds the play length; ``-f null -`` decodes and discards (no disk, no display)
    so only the network pull is exercised. Banner/log noise is silenced."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re", "-i", burst.stream,
        "-t", str(burst.duration_s),
        "-f", "null", "-",
    ]


# --------------------------------------------------------------------------- #
# Profiles: each draws its own burst and turns it into a command
# --------------------------------------------------------------------------- #

class Profile(Protocol):
    """One traffic kind. ``weight`` sets how often the loop draws it; ``draw_command``
    consumes the RNG to produce one burst's ready-to-run argv."""
    name: str
    weight: float

    def draw_command(self, rng: random.Random) -> list[str]: ...


class IperfRunner:
    name = PROFILE_IPERF

    def __init__(self, config: IperfProfile) -> None:
        self._config = config
        self.weight = config.weight

    def draw_burst(self, rng: random.Random) -> IperfBurst:
        c = self._config
        return IperfBurst(
            duration_s=rng.randint(c.burst_s[0], c.burst_s[1]),
            rate_mbps=round(rng.uniform(c.rate_mbps[0], c.rate_mbps[1]), 1),
            protocol=rng.choice(c.protocols),
            port=rng.choice(c.ports),
            reverse=rng.random() < c.reverse_prob,
        )

    def draw_command(self, rng: random.Random) -> list[str]:
        return build_iperf_command(self.draw_burst(rng), self._config.target)


class DownloadRunner:
    name = PROFILE_DOWNLOAD

    def __init__(self, config: DownloadProfile) -> None:
        self._config = config
        self.weight = config.weight

    def draw_burst(self, rng: random.Random) -> DownloadBurst:
        c = self._config
        return DownloadBurst(
            url=rng.choice(c.urls),
            rate_mbps=round(rng.uniform(c.rate_mbps[0], c.rate_mbps[1]), 1),
            max_time_s=rng.randint(c.max_time_s[0], c.max_time_s[1]),
        )

    def draw_command(self, rng: random.Random) -> list[str]:
        return build_curl_command(self.draw_burst(rng))


class VideoRunner:
    name = PROFILE_VIDEO

    def __init__(self, config: VideoProfile) -> None:
        self._config = config
        self.weight = config.weight

    def draw_burst(self, rng: random.Random) -> VideoBurst:
        c = self._config
        return VideoBurst(
            stream=rng.choice(c.streams),
            duration_s=rng.randint(c.duration_s[0], c.duration_s[1]),
        )

    def draw_command(self, rng: random.Random) -> list[str]:
        return build_ffmpeg_command(self.draw_burst(rng))


def _make_runner(profile: object) -> Profile:
    if isinstance(profile, IperfProfile):
        return IperfRunner(profile)
    if isinstance(profile, DownloadProfile):
        return DownloadRunner(profile)
    if isinstance(profile, VideoProfile):
        return VideoRunner(profile)
    raise TypeError(f"unknown noise profile: {type(profile).__name__}")


# --------------------------------------------------------------------------- #
# The seeded burst/idle loop
# --------------------------------------------------------------------------- #

class NoiseGenerator:
    """The seeded burst/idle loop. ``run_forever`` is the front door."""

    def __init__(self, config: NoiseConfig, *, run_command: RunCommand,
                 sleep: Sleep = time.sleep, seed_offset: int = 0) -> None:
        self._config = config
        self._runners = [_make_runner(p) for p in config.profiles()]
        self._run_command = run_command
        self._sleep = sleep
        # ``seed_offset`` lets a *second* noise generator run the same config but draw a
        # different burst/URL sequence, so two generators don't hit the same host in
        # lockstep (which would concentrate load and trip a rate limit). 0 = generator #1.
        self._seed = config.seed + seed_offset
        self._rng = random.Random(self._seed)

    @classmethod
    def from_env(cls) -> "NoiseGenerator":
        """Build for VM5: read ``config/noise.json`` via the instance-role S3, run real
        commands (iperf3 / curl / ffmpeg).

        ``NOISE_SEED_OFFSET`` (default 0) desynchronizes a second generator — run it with
        ``NOISE_SEED_OFFSET=1 python3 -m client.noise`` on the extra noise node."""
        config = SessionStore().read_noise_config()
        seed_offset = int(os.environ.get("NOISE_SEED_OFFSET", "0"))
        return cls(config, run_command=_run_command_subprocess, seed_offset=seed_offset)

    # --- front door -------------------------------------------------------- #

    def run_forever(self) -> None:
        """Fire bursts with random gaps between, forever (stopped by hand / container stop)."""
        self._log(f"starting: {len(self._runners)} profiles, seed {self._seed}. "
                  f"One line per burst follows — steady lines = alive, a stall = frozen.")
        while True:
            self.run_cycle()

    def run_cycle(self) -> None:
        """One cycle: pick a profile by weight, run its drawn burst to completion, then
        idle a drawn gap.

        The RNG is consumed pick-then-burst-then-gap, the same order every cycle, so the
        whole mixed sequence is reproducible from the seed. A ``start``/``done`` line
        brackets the burst so a freeze is visible: a ``start`` with no matching ``done``
        is the loop stuck inside that command."""
        runner = self.pick_runner()
        argv = runner.draw_command(self._rng)
        self._log(f"start {runner.name}: {' '.join(argv)}")
        started = time.monotonic()
        self._run_command(argv)
        gap = self.draw_gap()
        self._log(f"done  {runner.name} in {time.monotonic() - started:.1f}s; idle {gap}s")
        self._sleep(gap)

    def _log(self, msg: str) -> None:
        """One timestamped line, flushed immediately so it shows even when piped to a file."""
        print(f"[noise] {time.strftime('%H:%M:%S')} {msg}", flush=True)

    # --- the seeded draws (public so they can be checked directly) --------- #

    def pick_runner(self) -> Profile:
        """Draw one profile, weighted by each profile's ``weight``. Runners are in a
        fixed order (see ``NoiseConfig.profiles``) so the choice is reproducible."""
        total = sum(r.weight for r in self._runners)
        r = self._rng.random() * total
        acc = 0.0
        for runner in self._runners:
            acc += runner.weight
            if r < acc:
                return runner
        return self._runners[-1]  # float rounding guard: land on the last

    def draw_gap(self) -> float:
        """Draw one idle gap (seconds) from the configured range."""
        return round(self._rng.uniform(self._config.gap_s[0], self._config.gap_s[1]), 1)


# A single burst must never wedge the forever-loop. Every burst is wall-clock bounded
# above the longest legitimate one (video tops out at 90s) so a command that hangs — most
# often ffmpeg blocking on a stalled HLS input, where -t bounds *output* time and so never
# fires when no input arrives — is killed and the loop moves on.
_BURST_TIMEOUT_S = 120

# A download burst that moved fewer than this many bytes did ~nothing real: an HTTP error
# (403/404), a host throttling us (429), or a failed connect. The smallest *legitimate*
# burst still pulls hundreds of KB (rate floor 0.5 Mbps over the min window), so this
# cleanly separates a dead burst from a successful one — including the case where curl
# exits non-zero because it hit --max-time mid-transfer having already moved megabytes.
_MIN_DOWNLOAD_BYTES = 64 * 1024


def _curl_bytes_moved(stdout: bytes | None) -> int:
    """Bytes curl reported via ``-w %{size_download}`` (0 if missing/unparseable)."""
    if not stdout:
        return 0
    try:
        return int(float(stdout.split()[-1]))
    except (ValueError, IndexError):
        return 0


def _run_command_subprocess(argv: list[str]) -> None:
    """Run one real traffic burst, blocking until it finishes.

    A bad burst must not kill the loop — it just becomes a quieter stretch in the capture,
    which is fine. The ways a burst goes bad are all absorbed here: a hang (no data ever
    arrives — bounded by ``timeout`` then killed), a missing program (iperf3/curl/ffmpeg not
    installed — ``OSError``), and a burst that moved ~no traffic. Without this the loop would
    freeze or crash on the first such burst and noise would silently stop.

    Failures are logged loudly, never swallowed — a *silent* dead burst is exactly how the
    dead-download bug hid before. **Downloads are judged by bytes moved, not exit code:**
    curl returns non-zero when it stops a transfer at ``--max-time`` even though it pulled
    megabytes (the desired long burst), so only a near-zero byte count is a real failure
    (HTTP error / throttle / dead host). Other tools are judged by exit code as before."""
    import subprocess

    is_curl = bool(argv) and argv[0] == "curl"
    try:
        # Discard the child's own output (iperf's per-second wall of text, ffmpeg banners)
        # so the console shows only this loop's heartbeat. curl's stdout is the one
        # exception: it carries the -w byte count we need, so capture it.
        result = subprocess.run(
            argv, check=False, timeout=_BURST_TIMEOUT_S,
            stdout=subprocess.PIPE if is_curl else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print(f"[noise] burst exceeded {_BURST_TIMEOUT_S}s and was killed: {argv[0]}", flush=True)
        return
    except OSError as err:
        print(f"[noise] burst could not run ({argv[0]}): {err}", flush=True)
        return

    if is_curl:
        moved = _curl_bytes_moved(result.stdout)
        if moved < _MIN_DOWNLOAD_BYTES:
            print(f"[noise] download FAILED (moved {moved} B, rc={result.returncode}): "
                  f"{argv[-1]}", flush=True)
    elif result.returncode != 0:
        print(f"[noise] burst FAILED rc={result.returncode} (moved ~no traffic): "
              f"{' '.join(argv)}", flush=True)


def main() -> None:
    NoiseGenerator.from_env().run_forever()


if __name__ == "__main__":
    main()