"""The operational config for VM5's background-noise generator.

One file in S3 — ``config/noise.json`` — is the single source of truth for the noise
(REFACTOR_DESIGN.md decision 10). Two very different consumers read it, and they must
never drift apart:

* **VM5** reads the *whole* config to actually *run* the noise: which iperf server to
  hit, which ports, how hard to push, how long each burst lasts, how long to idle
  between bursts, and the seed that makes the whole loop reproducible.
* **VM4** reads the same config only to *record* what VM5 is doing into each session's
  spec/manifest, via :meth:`NoiseConfig.to_noise_block` — the small recorded subset
  (``enabled``/``profile``/``target``/``ports``) the offline labeler needs to tag noise
  flows by destination IP. If VM4 recorded a *different* target IP than VM5 actually
  used, noise would be mislabeled invisibly; reading one shared file makes that drift
  impossible.

This is shared contract, like :mod:`common.schema`, so it lives in ``common`` where both
VM4 and VM5 can import it. It is a pure shape — nothing here talks to AWS or runs iperf.
The richer *operational* fields (rate/burst/gap ranges + seed) live only here; they map
*into* the existing :class:`~common.schema.NoiseBlock`'s recorded subset, never the other
way round (a ``NoiseBlock`` cannot reconstruct the ranges, by design — it is the label,
not the recipe).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.schema import NoiseBlock

PROFILE_IPERF = "iperf"

# iperf carries each transfer over one transport; we vary it per burst so "noise" is not
# a single learnable signature (decision 10).
PROTO_TCP = "tcp"
PROTO_UDP = "udp"
PROTOCOLS = frozenset({PROTO_TCP, PROTO_UDP})


@dataclass
class NoiseConfig:
    """How VM5 generates background iperf noise, and what VM4 records about it.

    Every per-burst knob is a *range* (or a *set* to draw from) rather than a fixed
    value, so the background traffic is varied — a constant rate/port/protocol would
    itself be a pattern a model could latch onto. ``seed`` makes the whole burst/idle
    loop reproducible and describable in the thesis.

    Fields:
      * ``target``         — the dedicated internet iperf server's address (the labeler's
                             anchor: ``src=VM5 & dst=target`` is noise).
      * ``ports``          — the server ports to spread bursts across.
      * ``protocols``      — which transports to draw from (``tcp`` / ``udp``).
      * ``rate_mbps``      — [min, max] push rate per burst, in Mbps.
      * ``burst_s``        — [min, max] length of one iperf transfer, in whole seconds
                             (iperf ``-t`` takes integer seconds).
      * ``gap_s``          — [min, max] idle time between bursts, in seconds.
      * ``reverse_prob``   — chance a burst is a *download* (server→VM5, iperf ``-R``)
                             rather than an upload; gives directional variety.
      * ``seed``           — seeds the burst/idle RNG.
    """

    target: str
    ports: list[int]
    protocols: list[str]
    rate_mbps: tuple[float, float]
    burst_s: tuple[int, int]
    gap_s: tuple[float, float]
    reverse_prob: float
    seed: int

    def __post_init__(self) -> None:
        if not self.target:
            raise ValueError("target (iperf server address) must be set")
        if not self.ports:
            raise ValueError("need at least one port")
        if not self.protocols:
            raise ValueError("need at least one protocol")
        bad = set(self.protocols) - PROTOCOLS
        if bad:
            raise ValueError(f"protocols must be drawn from {sorted(PROTOCOLS)}, got {sorted(bad)}")
        _check_range("rate_mbps", self.rate_mbps, positive=True)
        # The burst rate is drawn then rounded to 0.1 Mbps; a floor that rounds to 0
        # would emit ``iperf3 -b 0M``, which iperf reads as UNLIMITED — VM5 would blast
        # the link at line rate and drown the very Zoom media being captured. Reject any
        # floor that can round to zero (the smallest possible drawn rate is round(min)).
        if round(self.rate_mbps[0], 1) <= 0:
            raise ValueError(
                f"rate_mbps min must round to >= 0.1 Mbps (else iperf -b 0M = unlimited), "
                f"got {self.rate_mbps[0]}"
            )
        _check_range("burst_s", self.burst_s, positive=True)
        _check_range("gap_s", self.gap_s, positive=False)  # a zero-length gap is allowed
        if not (0.0 <= self.reverse_prob <= 1.0):
            raise ValueError(f"reverse_prob must be in [0, 1], got {self.reverse_prob}")

    def to_noise_block(self) -> NoiseBlock:
        """The recorded subset VM4 stamps into the spec/manifest roster (decision 10).

        Only the facts the offline labeler needs to separate noise flows survive here:
        the profile and the destination ``target``/``ports``. The ranges and seed are
        deliberately *not* in the label — ``intensity`` carries a human-readable summary
        of the rate range for reproducibility, but the authoritative recipe stays in this
        config file (also snapshotted into ``manifest.noise``)."""
        lo, hi = self.rate_mbps
        return NoiseBlock(
            enabled=True,
            profile=PROFILE_IPERF,
            target=self.target,
            ports=",".join(str(p) for p in self.ports),
            intensity=f"{_g(lo)}-{_g(hi)}Mbps",
            source_ips=[],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "ports": list(self.ports),
            "protocols": list(self.protocols),
            "rate_mbps": list(self.rate_mbps),
            "burst_s": list(self.burst_s),
            "gap_s": list(self.gap_s),
            "reverse_prob": self.reverse_prob,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NoiseConfig":
        return cls(
            target=d["target"],
            ports=[int(p) for p in d["ports"]],
            protocols=[str(p) for p in d["protocols"]],
            rate_mbps=(float(d["rate_mbps"][0]), float(d["rate_mbps"][1])),
            burst_s=(int(d["burst_s"][0]), int(d["burst_s"][1])),
            gap_s=(float(d["gap_s"][0]), float(d["gap_s"][1])),
            reverse_prob=float(d["reverse_prob"]),
            seed=int(d["seed"]),
        )


def _check_range(name: str, rng: tuple[float, float], *, positive: bool) -> None:
    lo, hi = rng
    if lo > hi:
        raise ValueError(f"{name} min must be <= max, got {rng}")
    if positive and lo <= 0:
        raise ValueError(f"{name} min must be positive, got {lo}")
    if not positive and lo < 0:
        raise ValueError(f"{name} min must be >= 0, got {lo}")


def _g(x: float) -> str:
    """Format a number without a trailing ``.0`` (10.0 -> ``10``, 12.5 -> ``12.5``)."""
    return f"{x:g}"