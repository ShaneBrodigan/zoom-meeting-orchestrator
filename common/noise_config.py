"""The operational config for VM5's background-noise generator.

One file in S3 — ``config/noise.json`` — is the single source of truth for the noise
(REFACTOR_DESIGN.md decision 10). Two very different consumers read it, and they must
never drift apart:

* **VM5** reads the *whole* config to actually *run* the noise: which traffic profiles
  to mix (iperf throughput, web downloads, video streaming), how hard each pushes, how
  long each burst lasts, how long to idle between bursts, and the seed that makes the
  whole loop reproducible.
* **VM4** reads the same config only to *record* what VM5 is doing into each session's
  spec/manifest, via :meth:`NoiseConfig.to_noise_block` (the small recorded subset the
  manifest roster carries) and :meth:`NoiseConfig.to_dict` (the full recipe snapshotted
  into ``manifest.noise`` for reproducibility).

This is shared contract, like :mod:`common.schema`, so it lives in ``common`` where both
VM4 and VM5 can import it. It is a pure shape — nothing here talks to AWS, runs a command,
or owns the burst/idle RNG (that lives in :mod:`client.noise`).

**Why per-profile params (decision 10 + the realistic-noise convergence).** The original
config carried one flat set of iperf knobs. Real background traffic is more than raw
throughput, so the config now holds *several* traffic profiles, each with its own params
and a relative ``weight`` saying how often it is drawn. iperf was the first profile; web
downloads (curl) and video streaming (ffmpeg pulling HLS) are the second and third — the
real callers that justify a profile split (before them, building a plugin layer would have
been speculative). Every per-burst knob is a *range* (or a *set* to draw from) so the
traffic is varied — a constant rate/port/length would itself be a learnable pattern.
``seed`` makes the whole burst/idle loop reproducible and describable in the thesis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.schema import NoiseBlock

PROFILE_IPERF = "iperf"
PROFILE_DOWNLOAD = "download"
PROFILE_VIDEO = "video"

# iperf carries each transfer over one transport; we vary it per burst so "noise" is not
# a single learnable signature (decision 10).
PROTO_TCP = "tcp"
PROTO_UDP = "udp"
PROTOCOLS = frozenset({PROTO_TCP, PROTO_UDP})

# A drawn Mbps rate is rounded to 0.1 before use; a floor that rounds to 0 would emit an
# *unlimited*-rate transfer (``iperf3 -b 0M`` = line rate; ``curl --limit-rate 0`` = no
# cap), letting VM5 blast the link and drown the very Zoom media being captured. Reject
# any rate floor that can round to zero (the smallest drawn rate is round(min, 1)).
_MIN_RATE_MBPS = 0.1


@dataclass
class IperfProfile:
    """Raw-throughput bursts via iperf3 against the dedicated internet server.

    ``target``/``ports`` are also the manifest's recorded iperf anchor: even once noise
    runs concurrently on a VoIP VM (a future flip), its iperf flows stay separable from
    the call by that 5-tuple (decision 10)."""

    weight: float
    target: str
    ports: list[int]
    protocols: list[str]
    rate_mbps: tuple[float, float]
    burst_s: tuple[int, int]
    reverse_prob: float

    def __post_init__(self) -> None:
        _check_weight(PROFILE_IPERF, self.weight)
        if not self.target:
            raise ValueError("iperf target (server address) must be set")
        if not self.ports:
            raise ValueError("iperf needs at least one port")
        if not self.protocols:
            raise ValueError("iperf needs at least one protocol")
        bad = set(self.protocols) - PROTOCOLS
        if bad:
            raise ValueError(f"iperf protocols must be drawn from {sorted(PROTOCOLS)}, "
                             f"got {sorted(bad)}")
        _check_range("iperf rate_mbps", self.rate_mbps, positive=True)
        _check_rate_floor("iperf rate_mbps", self.rate_mbps)
        _check_range("iperf burst_s", self.burst_s, positive=True)
        if not (0.0 <= self.reverse_prob <= 1.0):
            raise ValueError(f"iperf reverse_prob must be in [0, 1], got {self.reverse_prob}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight,
            "target": self.target,
            "ports": list(self.ports),
            "protocols": list(self.protocols),
            "rate_mbps": list(self.rate_mbps),
            "burst_s": list(self.burst_s),
            "reverse_prob": self.reverse_prob,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IperfProfile":
        return cls(
            weight=float(d["weight"]),
            target=str(d["target"]),
            ports=[int(p) for p in d["ports"]],
            protocols=[str(p) for p in d["protocols"]],
            rate_mbps=(float(d["rate_mbps"][0]), float(d["rate_mbps"][1])),
            burst_s=(int(d["burst_s"][0]), int(d["burst_s"][1])),
            reverse_prob=float(d["reverse_prob"]),
        )


@dataclass
class DownloadProfile:
    """Web file downloads via curl from a curated, pinned set of public URLs.

    Hits *real, varied* hosts (no single learnable endpoint), pinned so runs are
    reproducible and the files don't vanish mid-dataset. ``rate_mbps`` caps the download
    speed (curl ``--limit-rate``) and ``max_time_s`` bounds how long one download runs,
    so each flow is long enough to carry a real TLS handshake plus data packets."""

    weight: float
    urls: list[str]
    rate_mbps: tuple[float, float]
    max_time_s: tuple[int, int]

    def __post_init__(self) -> None:
        _check_weight(PROFILE_DOWNLOAD, self.weight)
        if not self.urls:
            raise ValueError("download needs at least one URL")
        _check_range("download rate_mbps", self.rate_mbps, positive=True)
        _check_rate_floor("download rate_mbps", self.rate_mbps)
        _check_range("download max_time_s", self.max_time_s, positive=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight,
            "urls": list(self.urls),
            "rate_mbps": list(self.rate_mbps),
            "max_time_s": list(self.max_time_s),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DownloadProfile":
        return cls(
            weight=float(d["weight"]),
            urls=[str(u) for u in d["urls"]],
            rate_mbps=(float(d["rate_mbps"][0]), float(d["rate_mbps"][1])),
            max_time_s=(int(d["max_time_s"][0]), int(d["max_time_s"][1])),
        )


@dataclass
class VideoProfile:
    """Video streaming via ffmpeg pulling a public HLS/DASH stream at real-time pace.

    ``-re`` makes ffmpeg pull at playback speed (segment, wait, segment) like a real
    player rather than a flat-out download; the decoded output is discarded — only the
    network traffic matters. ``duration_s`` is how many seconds of playback one burst
    streams. No rate knob: the stream's natural bitrate sets the pace."""

    weight: float
    streams: list[str]
    duration_s: tuple[int, int]

    def __post_init__(self) -> None:
        _check_weight(PROFILE_VIDEO, self.weight)
        if not self.streams:
            raise ValueError("video needs at least one stream URL")
        _check_range("video duration_s", self.duration_s, positive=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "weight": self.weight,
            "streams": list(self.streams),
            "duration_s": list(self.duration_s),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VideoProfile":
        return cls(
            weight=float(d["weight"]),
            streams=[str(s) for s in d["streams"]],
            duration_s=(int(d["duration_s"][0]), int(d["duration_s"][1])),
        )


@dataclass
class NoiseConfig:
    """How VM5 generates background noise, and what VM4 records about it.

    Holds the shared loop knobs (``seed``, ``gap_s``) plus the set of traffic profiles to
    mix. Profiles are optional — a config may run any non-empty subset — but at least one
    must be present, because an empty noise generator is almost certainly a misconfigured
    one (decision 10: noise must blanket the pre-roll/gaps/post-roll, so it must do
    *something*).

    Fields:
      * ``seed``     — seeds the burst/idle + profile-choice RNG.
      * ``gap_s``    — [min, max] idle time between bursts, in seconds (shared by all
                       profiles; a zero-length gap is allowed).
      * ``iperf`` / ``download`` / ``video`` — the per-profile params, or ``None`` if that
                       profile is not in the mix.
    """

    seed: int
    gap_s: tuple[float, float]
    iperf: IperfProfile | None = None
    download: DownloadProfile | None = None
    video: VideoProfile | None = None

    def __post_init__(self) -> None:
        _check_range("gap_s", self.gap_s, positive=False)  # a zero-length gap is allowed
        if not self.profiles():
            raise ValueError("noise config must enable at least one traffic profile "
                             "(iperf / download / video)")

    def profiles(self) -> list[Any]:
        """The present profiles, in a fixed order so the seeded weighted draw in
        :mod:`client.noise` is reproducible."""
        return [p for p in (self.iperf, self.download, self.video) if p is not None]

    def to_noise_block(self) -> NoiseBlock:
        """The recorded subset VM4 stamps into the spec/manifest roster (decision 10).

        The offline labeler tags noise by *source* now (any flow from a ``zoom_role:none``
        VM is noise), so it does not need this anchor for VM5. The iperf ``target``/``ports``
        are still recorded because they remain the *only* way to separate noise from the
        call on a future concurrent-noise VoIP VM. ``profile`` summarises the active mix
        and ``intensity`` the rate ranges, for a human reading the manifest; the
        authoritative recipe is the full config in ``manifest.noise``."""
        names = [_profile_name(p) for p in self.profiles()]
        iperf = self.iperf
        return NoiseBlock(
            enabled=True,
            profile="+".join(names),
            target=iperf.target if iperf else None,
            ports=",".join(str(p) for p in iperf.ports) if iperf else None,
            intensity=self._intensity_summary(),
            source_ips=[],
        )

    def _intensity_summary(self) -> str:
        parts: list[str] = []
        if self.iperf:
            lo, hi = self.iperf.rate_mbps
            parts.append(f"iperf {_g(lo)}-{_g(hi)}Mbps")
        if self.download:
            lo, hi = self.download.rate_mbps
            parts.append(f"download {_g(lo)}-{_g(hi)}Mbps")
        if self.video:
            lo, hi = self.video.duration_s
            parts.append(f"video {lo}-{hi}s")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        profiles: dict[str, Any] = {}
        if self.iperf:
            profiles[PROFILE_IPERF] = self.iperf.to_dict()
        if self.download:
            profiles[PROFILE_DOWNLOAD] = self.download.to_dict()
        if self.video:
            profiles[PROFILE_VIDEO] = self.video.to_dict()
        return {"seed": self.seed, "gap_s": list(self.gap_s), "profiles": profiles}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NoiseConfig":
        profiles = d.get("profiles", {})
        return cls(
            seed=int(d["seed"]),
            gap_s=(float(d["gap_s"][0]), float(d["gap_s"][1])),
            iperf=IperfProfile.from_dict(profiles[PROFILE_IPERF])
            if PROFILE_IPERF in profiles else None,
            download=DownloadProfile.from_dict(profiles[PROFILE_DOWNLOAD])
            if PROFILE_DOWNLOAD in profiles else None,
            video=VideoProfile.from_dict(profiles[PROFILE_VIDEO])
            if PROFILE_VIDEO in profiles else None,
        )


def _profile_name(profile: Any) -> str:
    return {
        IperfProfile: PROFILE_IPERF,
        DownloadProfile: PROFILE_DOWNLOAD,
        VideoProfile: PROFILE_VIDEO,
    }[type(profile)]


def _check_weight(name: str, weight: float) -> None:
    if weight <= 0:
        raise ValueError(f"{name} weight must be positive, got {weight}")


def _check_range(name: str, rng: tuple[float, float], *, positive: bool) -> None:
    lo, hi = rng
    if lo > hi:
        raise ValueError(f"{name} min must be <= max, got {rng}")
    if positive and lo <= 0:
        raise ValueError(f"{name} min must be positive, got {lo}")
    if not positive and lo < 0:
        raise ValueError(f"{name} min must be >= 0, got {lo}")


def _check_rate_floor(name: str, rng: tuple[float, float]) -> None:
    if round(rng[0], 1) < _MIN_RATE_MBPS:
        raise ValueError(
            f"{name} min must round to >= {_g(_MIN_RATE_MBPS)} Mbps "
            f"(else an unlimited-rate transfer floods the link), got {rng[0]}"
        )


def _g(x: float) -> str:
    """Format a number without a trailing ``.0`` (10.0 -> ``10``, 12.5 -> ``12.5``)."""
    return f"{x:g}"
