"""The offline labeler: manifest + pcap in, labels.json out.

This is the "answer key writer" of REFACTOR_DESIGN.md decision 6. VM4 records only
*raw facts* in ``manifest.json``; this script — versioned, run offline, no AWS —
derives the labels from those facts plus the packet capture:

* a **timeline**: windows of "how many machines were on the call" (0-party
  background-only pre-roll, 1-party join ramp, 2-party, ... and back down), built
  from the real join/leave timestamps the bots heartbeated;
* **flow labels**: every flow in the pcap (one flow = one conversation between two
  endpoints — the address+port pair on each side plus the protocol) tagged as
  ``noise`` / ``zoom_media`` / ``zoom_signaling`` / ``other``, with the rule that
  fired recorded so a surprising label is auditable.

Noise is tagged by the (source VM, iperf server) address pair recorded in the
manifest roster's noise blocks — the one property that survives the per-burst
port/rate/protocol randomization (decision 10). Because the match is per roster
entry (and covers any extra ``source_ips``), the same rule keeps working when
noise later runs concurrently on a VoIP VM or from extra ENIs.

**Hygiene boundary (decision 6/10):** raw IPs appear throughout this output ON
PURPOSE. Labels are the researcher's offline answer key and may use oracle knowledge
like server addresses. The "no memorizable endpoint addresses" rule applies to
*features* (model inputs), which are extracted in a separate, later step — never
feed this file's IPs (including the Zoom relay ranges) to the model as features.

Pure core, injected edge: ``derive_labels()`` takes the manifest plus any iterable
of ``PacketRecord`` — tests synthesize packets directly; only ``read_pcap()``
touches scapy and the real file. Front door for a session folder:

    python -m labeler.derive_labels <dir-with-manifest.json-and-capture.pcap>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from common.schema import Manifest, ROLE_NONE

# Bump when the label semantics change in a way that makes old labels.json files
# non-comparable. Stamped into every output so a relabeled dataset is self-describing.
LABELER_VERSION = 1

LABEL_NOISE = "noise"
LABEL_ZOOM_MEDIA = "zoom_media"
LABEL_ZOOM_SIGNALING = "zoom_signaling"
LABEL_OTHER = "other"

# Network housekeeping that is neither Zoom nor noise: DNS, DHCP, NTP/chrony, mDNS.
_HOUSEKEEPING_PORTS = frozenset({53, 67, 68, 123, 5353})


# --------------------------------------------------------------------------- #
# Input shape (what the pcap edge produces, what tests synthesize)
# --------------------------------------------------------------------------- #

@dataclass
class PacketRecord:
    """One captured packet, reduced to the fields labeling needs.

    ``ts`` is epoch seconds (pcap timestamps and manifest timestamps share the
    chrony-aligned clock, so they are directly comparable). Ports are ``None``
    for protocols without them (e.g. ICMP)."""
    ts: float
    src_ip: str
    dst_ip: str
    proto: str  # "tcp" | "udp" | other (e.g. "icmp")
    src_port: int | None
    dst_port: int | None
    length: int


# --------------------------------------------------------------------------- #
# Output shapes (what labels.json holds)
# --------------------------------------------------------------------------- #

@dataclass
class TimelineWindow:
    """During [t0, t1), exactly ``ips`` were on the call."""
    t0: float
    t1: float
    ips: list[str]

    @property
    def party_count(self) -> int:
        return len(self.ips)

    def to_dict(self) -> dict[str, Any]:
        return {"t0": self.t0, "t1": self.t1,
                "party_count": self.party_count, "ips": list(self.ips)}


@dataclass
class FlowLabel:
    """One labeled flow. Endpoints are stored in a canonical order (a <= b) so both
    directions of a conversation land in the same record; ``rule`` names the rule
    that produced ``label`` so any surprising tag can be traced."""
    proto: str
    ip_a: str
    port_a: int | None
    ip_b: str
    port_b: int | None
    label: str
    rule: str
    packets: int
    bytes: int
    t_first: float
    t_last: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "proto": self.proto,
            "ip_a": self.ip_a, "port_a": self.port_a,
            "ip_b": self.ip_b, "port_b": self.port_b,
            "label": self.label, "rule": self.rule,
            "packets": self.packets, "bytes": self.bytes,
            "t_first": self.t_first, "t_last": self.t_last,
        }


@dataclass
class Labels:
    """The full derived-labels document (``labels.json``)."""
    session_id: str
    timeline: list[TimelineWindow]
    flows: list[FlowLabel]
    warnings: list[str] = field(default_factory=list)
    labeler_version: int = LABELER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "labeler_version": self.labeler_version,
            "session_id": self.session_id,
            "timeline": [w.to_dict() for w in self.timeline],
            "flows": [f.to_dict() for f in self.flows],
            "warnings": list(self.warnings),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# --------------------------------------------------------------------------- #
# Front door
# --------------------------------------------------------------------------- #

def derive_labels(manifest: Manifest, packets: Iterable[PacketRecord]) -> Labels:
    """Compute the timeline and flow labels for one session. Pure: no file or
    AWS access — feed it ``read_pcap(...)`` for a real capture or any list of
    ``PacketRecord`` in tests."""
    timeline, timeline_warnings = _derive_timeline(manifest)
    flows, flow_warnings = _label_flows(manifest, packets)
    return Labels(
        session_id=manifest.session_id,
        timeline=timeline,
        flows=flows,
        warnings=timeline_warnings + flow_warnings,
    )


# --------------------------------------------------------------------------- #
# Timeline: capture window + join/leave facts -> party-count windows
# --------------------------------------------------------------------------- #

def _derive_timeline(manifest: Manifest) -> tuple[list[TimelineWindow], list[str]]:
    t_start = manifest.capture.t_start
    t_stop = manifest.capture.t_stop
    warnings: list[str] = []

    # Resolve each participant to a concrete [join, leave) interval, clamped to the
    # capture window. Degraded facts (missing events) become warnings, not guesses
    # hidden in the data.
    intervals: list[tuple[str, float, float]] = []
    for jl in manifest.joins_leaves:
        if jl.t_join is None and jl.t_leave is None:
            warnings.append(f"{jl.ip}: no join recorded; excluded from timeline")
            continue
        if jl.t_join is None:
            # The leave event proves it was on the call; capture start is the
            # earliest defensible bound for when that began.
            warnings.append(f"{jl.ip}: no join recorded but a leave is; "
                            "treated as on call from capture start")
            join = t_start
        else:
            join = max(jl.t_join, t_start)
        if jl.t_leave is None:
            # The REST hard-stop ends media at meeting end regardless of bot health,
            # so capture stop is the latest defensible bound.
            warnings.append(f"{jl.ip}: no leave recorded; treated as on call until capture stop")
            leave = t_stop
        else:
            leave = min(jl.t_leave, t_stop)
        if join >= leave:
            warnings.append(f"{jl.ip}: join/leave window empty after clamping to capture; excluded")
            continue
        intervals.append((jl.ip, join, leave))

    # Party count only changes at a join/leave instant, so those instants (plus the
    # capture edges) are the only window boundaries needed.
    bounds = sorted({t_start, t_stop,
                     *(j for _, j, _ in intervals),
                     *(l for _, _, l in intervals)})
    windows: list[TimelineWindow] = []
    for t0, t1 in zip(bounds, bounds[1:]):
        mid = (t0 + t1) / 2
        ips = sorted(ip for ip, join, leave in intervals if join <= mid < leave)
        windows.append(TimelineWindow(t0=t0, t1=t1, ips=ips))

    # Simultaneous events (e.g. the REST hard-stop dropping everyone at once) can
    # leave adjacent windows with the same membership — merge them.
    merged: list[TimelineWindow] = []
    for w in windows:
        if merged and merged[-1].ips == w.ips:
            merged[-1].t1 = w.t1
        else:
            merged.append(w)
    return merged, warnings


# --------------------------------------------------------------------------- #
# Flows: group packets into conversations, then label each by rule
# --------------------------------------------------------------------------- #

def _label_flows(
    manifest: Manifest, packets: Iterable[PacketRecord]
) -> tuple[list[FlowLabel], list[str]]:
    # Oracle knowledge from the manifest roster (allowed here; see hygiene note up
    # top). Read from the typed RosterEntry/NoiseBlock shapes, not the free-form
    # manifest.noise summary dict, so the contract is schema-enforced.
    warnings: list[str] = []
    roster_ips = {e.ip for e in manifest.roster}
    voip_ips = {e.ip for e in manifest.roster if e.zoom_role != ROLE_NONE}
    noise_pairs: set[frozenset] = set()
    for e in manifest.roster:
        if not e.noise.enabled:
            continue
        if not e.noise.target:
            # Without the recorded anchor its iperf flows would silently land in
            # other labels — the "mislabel noise invisibly" trap of decision 10.
            warnings.append(f"{e.ip}: noise enabled but no target recorded; "
                            "its flows cannot be tagged noise")
            continue
        for source_ip in {e.ip, *e.noise.source_ips}:  # extra ENIs included
            noise_pairs.add(frozenset((source_ip, e.noise.target)))

    # Group both directions of a conversation under one canonical key.
    stats: dict[tuple, _FlowStats] = defaultdict(_FlowStats)
    for p in packets:
        a = (p.src_ip, p.src_port)
        b = (p.dst_ip, p.dst_port)
        if _endpoint_key(b) < _endpoint_key(a):
            a, b = b, a
        s = stats[(p.proto, a, b)]
        s.packets += 1
        s.bytes += p.length
        s.t_first = min(s.t_first, p.ts)
        s.t_last = max(s.t_last, p.ts)

    flows: list[FlowLabel] = []
    for (proto, (ip_a, port_a), (ip_b, port_b)), s in stats.items():
        label, rule = _classify_flow(
            proto, ip_a, port_a, ip_b, port_b,
            noise_pairs=noise_pairs, voip_ips=voip_ips, roster_ips=roster_ips,
        )
        flows.append(FlowLabel(
            proto=proto, ip_a=ip_a, port_a=port_a, ip_b=ip_b, port_b=port_b,
            label=label, rule=rule,
            packets=s.packets, bytes=s.bytes, t_first=s.t_first, t_last=s.t_last,
        ))
    flows.sort(key=lambda f: f.t_first)
    return flows, warnings


@dataclass
class _FlowStats:
    packets: int = 0
    bytes: int = 0
    t_first: float = float("inf")
    t_last: float = float("-inf")


def _endpoint_key(endpoint: tuple[str, int | None]) -> tuple[str, int]:
    ip, port = endpoint
    return ip, -1 if port is None else port


def _classify_flow(
    proto: str,
    ip_a: str, port_a: int | None,
    ip_b: str, port_b: int | None,
    *,
    noise_pairs: set[frozenset],
    voip_ips: set[str],
    roster_ips: set[str],
) -> tuple[str, str]:
    """First matching rule wins; the rule name is recorded alongside the label.

    Noise is checked first so that, even when noise later runs concurrently on a
    VoIP VM (or a burst happens to land on a Zoom-looking port), the recorded
    (source, iperf server) pair still claims the flow."""
    ports = {port_a, port_b}
    if frozenset((ip_a, ip_b)) in noise_pairs:
        return LABEL_NOISE, "noise-vm-to-iperf-server"
    if ports & _HOUSEKEEPING_PORTS:
        return LABEL_OTHER, "housekeeping-port"
    involves_voip = ip_a in voip_ips or ip_b in voip_ips
    if proto == "tcp" and 443 in ports and involves_voip:
        return LABEL_ZOOM_SIGNALING, "voip-client-tls-443"
    if proto == "udp" and involves_voip and (ip_a not in roster_ips or ip_b not in roster_ips):
        return LABEL_ZOOM_MEDIA, "voip-client-udp-external"
    return LABEL_OTHER, "unmatched"


# --------------------------------------------------------------------------- #
# The real-world edge: reading a pcap file (scapy lives only here)
# --------------------------------------------------------------------------- #

def read_pcap(path: str | Path) -> Iterator[PacketRecord]:
    """Stream a capture file (pcap or pcapng, as tshark writes) as PacketRecords.
    Non-IP frames are skipped; IP packets without TCP/UDP keep their protocol
    name and carry no ports."""
    from scapy.layers.inet import ICMP, IP, TCP, UDP
    from scapy.utils import PcapReader

    with PcapReader(str(path)) as reader:
        for pkt in reader:
            if IP not in pkt:
                continue
            ip = pkt[IP]
            if TCP in pkt:
                proto, sport, dport = "tcp", int(pkt[TCP].sport), int(pkt[TCP].dport)
            elif UDP in pkt:
                proto, sport, dport = "udp", int(pkt[UDP].sport), int(pkt[UDP].dport)
            elif ICMP in pkt:
                proto, sport, dport = "icmp", None, None
            else:
                proto, sport, dport = f"ip-proto-{int(ip.proto)}", None, None
            yield PacketRecord(
                ts=float(pkt.time),
                src_ip=ip.src, dst_ip=ip.dst,
                proto=proto, src_port=sport, dst_port=dport,
                # On-wire length from the capture record header: correct even under
                # a snaplen, and avoids re-serializing every packet.
                length=int(pkt.wirelen) if pkt.wirelen else len(pkt),
            )


# --------------------------------------------------------------------------- #
# CLI: python -m labeler.derive_labels <session_dir>
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive labels.json from a session folder holding "
                    "manifest.json and capture.pcap (downloaded from S3).",
    )
    parser.add_argument("session_dir", help="folder with manifest.json + capture.pcap")
    args = parser.parse_args(argv)

    session_dir = Path(args.session_dir)
    manifest = Manifest.from_json((session_dir / "manifest.json").read_text())
    labels = derive_labels(manifest, read_pcap(session_dir / "capture.pcap"))
    out = session_dir / "labels.json"
    out.write_text(labels.to_json())

    # A human-readable recap so the run can be eyeballed without opening the JSON.
    print(f"{manifest.session_id}: wrote {out}")
    print("timeline:")
    for w in labels.timeline:
        ips = ", ".join(w.ips) if w.ips else "-"
        print(f"  [{w.t0:.2f} .. {w.t1:.2f})  {w.party_count}-party  ({ips})")
    counts = Counter(f.label for f in labels.flows)
    print("flows:", dict(counts) if counts else "none")
    for warning in labels.warnings:
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())