"""Checkpoint tests for the offline labeler (labeler/derive_labels.py).

Pure, local, no AWS, no real pcap needed — packets are synthesized PacketRecords
fed to the front door. The CLI test writes a tiny real pcap via scapy to also
exercise the read_pcap edge. Run with:  pytest tests/test_labeler.py
"""

import json

from common import schema
from common.schema import (
    Capture,
    HeartbeatEvent,
    JoinLeave,
    Manifest,
    Meeting,
    NoiseBlock,
    RosterEntry,
    Seeds,
    Spec,
    Timing,
    Turns,
    TurnWindow,
)
from orchestrator.manifest import build_manifest
from labeler.derive_labels import (
    LABEL_NOISE,
    LABEL_OTHER,
    LABEL_ZOOM_MEDIA,
    LABEL_ZOOM_SIGNALING,
    LABELER_VERSION,
    PacketRecord,
    derive_labels,
    main,
)

HOST = "10.0.1.119"
JOINER2 = "10.0.2.67"
JOINER3 = "10.0.3.53"
NOISE_VM = "10.0.4.16"
IPERF_SERVER = "203.0.113.50"
ZOOM_RELAY = "144.195.1.2"


def make_manifest(
    joins_leaves=None,
    *,
    three_party=False,
    noise_on_host=False,
    noise_block=None,
) -> Manifest:
    if noise_block is None:
        noise_block = NoiseBlock(enabled=True, profile="iperf",
                                 target=IPERF_SERVER, ports="5201,5202,5203")
    roster = [
        RosterEntry(ip=HOST, zoom_role=schema.ROLE_HOST,
                    noise=noise_block if noise_on_host else NoiseBlock()),
        RosterEntry(ip=JOINER2, zoom_role=schema.ROLE_JOINER),
        RosterEntry(ip=NOISE_VM, zoom_role=schema.ROLE_NONE, noise=noise_block),
    ]
    if three_party:
        roster.insert(2, RosterEntry(ip=JOINER3, zoom_role=schema.ROLE_JOINER))
    return Manifest(
        session_id="sess-test",
        meeting_id="123456789",
        roster=roster,
        joins_leaves=joins_leaves or [],
        capture=Capture(t_start=0.0, t_stop=100.0, pcap_key="sessions/s/capture.pcap"),
        seeds=Seeds(turns=4711, timing=9001),
    )


def udp(src, dst, sport, dport, ts=1.0, length=100):
    return PacketRecord(ts=ts, src_ip=src, dst_ip=dst, proto="udp",
                        src_port=sport, dst_port=dport, length=length)


def tcp(src, dst, sport, dport, ts=1.0, length=100):
    return PacketRecord(ts=ts, src_ip=src, dst_ip=dst, proto="tcp",
                        src_port=sport, dst_port=dport, length=length)


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #

def test_two_party_timeline_ramps_up_and_down():
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=10.0, t_leave=80.0),
        JoinLeave(ip=JOINER2, t_join=20.0, t_leave=90.0),
    ])
    labels = derive_labels(m, [])
    got = [(w.t0, w.t1, w.party_count, w.ips) for w in labels.timeline]
    assert got == [
        (0.0, 10.0, 0, []),
        (10.0, 20.0, 1, [HOST]),
        (20.0, 80.0, 2, sorted([HOST, JOINER2])),
        (80.0, 90.0, 1, [JOINER2]),
        (90.0, 100.0, 0, []),
    ]
    assert labels.warnings == []


def test_three_party_overlap_window():
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=10.0, t_leave=70.0),
        JoinLeave(ip=JOINER2, t_join=15.0, t_leave=75.0),
        JoinLeave(ip=JOINER3, t_join=30.0, t_leave=60.0),
    ], three_party=True)
    labels = derive_labels(m, [])
    three = [w for w in labels.timeline if w.party_count == 3]
    assert len(three) == 1
    assert (three[0].t0, three[0].t1) == (30.0, 60.0)
    assert three[0].ips == sorted([HOST, JOINER2, JOINER3])


def test_simultaneous_hard_stop_leaves_merge_into_one_boundary():
    """All bots dropped at the same instant (the REST hard-stop) must not create
    zero-length or duplicate windows."""
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=10.0, t_leave=90.0),
        JoinLeave(ip=JOINER2, t_join=10.0, t_leave=90.0),
    ])
    labels = derive_labels(m, [])
    got = [(w.t0, w.t1, w.party_count) for w in labels.timeline]
    assert got == [(0.0, 10.0, 0), (10.0, 90.0, 2), (90.0, 100.0, 0)]


def test_missing_leave_extends_to_capture_stop_with_warning():
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=10.0, t_leave=None),
        JoinLeave(ip=JOINER2, t_join=20.0, t_leave=80.0),
    ])
    labels = derive_labels(m, [])
    last = labels.timeline[-1]
    assert (last.t0, last.t1, last.ips) == (80.0, 100.0, [HOST])
    assert any("no leave recorded" in w for w in labels.warnings)


def test_never_joined_excluded_with_warning():
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=10.0, t_leave=90.0),
        JoinLeave(ip=JOINER2, t_join=None, t_leave=None),
    ])
    labels = derive_labels(m, [])
    assert all(JOINER2 not in w.ips for w in labels.timeline)
    assert any("no join recorded" in w for w in labels.warnings)


def test_leave_without_join_counts_from_capture_start_with_warning():
    """A lost 'joined' heartbeat with a surviving 'left' proves the bot was on the
    call — it must count, from the earliest defensible bound (capture start)."""
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=10.0, t_leave=90.0),
        JoinLeave(ip=JOINER2, t_join=None, t_leave=80.0),
    ])
    labels = derive_labels(m, [])
    first = labels.timeline[0]
    assert (first.t0, first.ips) == (0.0, [JOINER2])
    two_party = [w for w in labels.timeline if w.party_count == 2]
    assert [(w.t0, w.t1) for w in two_party] == [(10.0, 80.0)]
    assert any("treated as on call from capture start" in w for w in labels.warnings)


def test_join_leave_clamped_to_capture_window():
    """Timestamps just outside the capture window (clock skew, late stop) must not
    create windows outside [t_start, t_stop)."""
    m = make_manifest([
        JoinLeave(ip=HOST, t_join=-5.0, t_leave=120.0),
    ])
    labels = derive_labels(m, [])
    got = [(w.t0, w.t1, w.party_count) for w in labels.timeline]
    assert got == [(0.0, 100.0, 1)]


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #

def test_flow_groups_both_directions_and_aggregates():
    packets = [
        udp(HOST, ZOOM_RELAY, 50000, 8801, ts=5.0, length=200),
        udp(ZOOM_RELAY, HOST, 8801, 50000, ts=6.0, length=300),
        udp(HOST, ZOOM_RELAY, 50000, 8801, ts=7.5, length=250),
    ]
    labels = derive_labels(make_manifest(), packets)
    assert len(labels.flows) == 1
    f = labels.flows[0]
    assert f.packets == 3
    assert f.bytes == 750
    assert (f.t_first, f.t_last) == (5.0, 7.5)
    assert {f.ip_a, f.ip_b} == {HOST, ZOOM_RELAY}


def test_noise_labeled_by_address_pair_in_both_directions():
    packets = [
        udp(NOISE_VM, IPERF_SERVER, 49152, 5203, ts=1.0),
        udp(IPERF_SERVER, NOISE_VM, 5203, 49152, ts=2.0),   # download burst
        tcp(NOISE_VM, IPERF_SERVER, 49200, 5207, ts=3.0),   # different proto/port
    ]
    labels = derive_labels(make_manifest(), packets)
    assert len(labels.flows) == 2
    assert all(f.label == LABEL_NOISE for f in labels.flows)
    assert all(f.rule == "noise-vm-to-iperf-server" for f in labels.flows)


def test_noise_rule_beats_zoom_rules_for_concurrent_noise_on_voip_vm():
    """Future flag-flip: the host runs noise too. Its iperf flow — even UDP on a
    Zoom-looking port — must label noise, not zoom_media."""
    m = make_manifest(noise_on_host=True)
    packets = [udp(HOST, IPERF_SERVER, 50000, 8801, ts=1.0)]
    labels = derive_labels(m, packets)
    assert labels.flows[0].label == LABEL_NOISE


def test_voip_vm_with_noise_keeps_its_zoom_flow_labeled_zoom():
    """The source rule is gated on zoom_role:none, so a VoIP VM that also runs noise
    (future flip) must keep its real Zoom media labeled zoom_media — only its flow to
    the iperf anchor is noise."""
    m = make_manifest(noise_on_host=True)
    labels = derive_labels(m, [udp(HOST, ZOOM_RELAY, 50000, 8801)])
    assert labels.flows[0].label == LABEL_ZOOM_MEDIA


def test_noise_from_extra_eni_source_ip_still_tagged():
    """Future flag-flip: multi-ENI noise. Bursts leaving from a recorded extra
    source IP must still match the noise rule."""
    eni_ip = "10.0.4.200"
    m = make_manifest(noise_block=NoiseBlock(
        enabled=True, profile="iperf", target=IPERF_SERVER,
        ports="5201,5202,5203", source_ips=[eni_ip],
    ))
    labels = derive_labels(m, [udp(eni_ip, IPERF_SERVER, 49152, 5202)])
    assert labels.flows[0].label == LABEL_NOISE


def test_noise_vm_without_anchor_still_tagged_by_source():
    """A pure-noise VM needs no iperf anchor: every flow it sources is noise, so a
    missing target is fine and silent — the source rule covers it."""
    m = make_manifest(noise_block=NoiseBlock(enabled=True, profile="download", target=None))
    labels = derive_labels(m, [udp(NOISE_VM, IPERF_SERVER, 49152, 5201)])
    assert labels.flows[0].label == LABEL_NOISE
    assert labels.flows[0].rule == "noise-from-noise-vm"
    assert labels.warnings == []


def test_voip_vm_noise_without_anchor_warns_instead_of_silent_mislabel():
    """On a VoIP VM the source rule can't fire (its IP also carries Zoom), so a noise
    block missing its anchor cannot be separated from the call — that must be loud,
    not an invisibly mislabeled dataset (decision 10)."""
    m = make_manifest(noise_on_host=True,
                      noise_block=NoiseBlock(enabled=True, profile="iperf", target=None))
    labels = derive_labels(m, [udp(HOST, IPERF_SERVER, 50000, 8801)])
    assert any("no target recorded" in w for w in labels.warnings)


def test_zoom_media_udp_between_voip_client_and_external():
    labels = derive_labels(make_manifest(), [udp(HOST, ZOOM_RELAY, 50000, 8801)])
    f = labels.flows[0]
    assert f.label == LABEL_ZOOM_MEDIA
    assert f.rule == "voip-client-udp-external"


def test_zoom_signaling_tcp_443():
    labels = derive_labels(make_manifest(), [tcp(JOINER2, ZOOM_RELAY, 51000, 443)])
    assert labels.flows[0].label == LABEL_ZOOM_SIGNALING


def test_dns_and_ntp_labeled_other_housekeeping():
    packets = [
        udp(HOST, "10.0.0.2", 50001, 53, ts=1.0),
        udp(HOST, "169.254.169.123", 50002, 123, ts=2.0),
    ]
    labels = derive_labels(make_manifest(), packets)
    assert all(f.label == LABEL_OTHER and f.rule == "housekeeping-port"
               for f in labels.flows)


def test_noise_vm_traffic_to_arbitrary_hosts_is_noise():
    """VM5 has no Zoom role, so everything it sends is noise — including web
    downloads / video to arbitrary hosts, not just iperf to the anchor server."""
    labels = derive_labels(make_manifest(), [udp(NOISE_VM, "8.8.8.8", 40000, 9999)])
    f = labels.flows[0]
    assert f.label == LABEL_NOISE
    assert f.rule == "noise-from-noise-vm"


def test_portless_packets_are_grouped_and_fall_through():
    packets = [
        PacketRecord(ts=1.0, src_ip=HOST, dst_ip="8.8.8.8", proto="icmp",
                     src_port=None, dst_port=None, length=84),
        PacketRecord(ts=2.0, src_ip="8.8.8.8", dst_ip=HOST, proto="icmp",
                     src_port=None, dst_port=None, length=84),
    ]
    labels = derive_labels(make_manifest(), packets)
    assert len(labels.flows) == 1
    assert labels.flows[0].label == LABEL_OTHER


def test_flows_sorted_by_first_packet_time():
    packets = [
        tcp(JOINER2, ZOOM_RELAY, 51000, 443, ts=9.0),
        udp(HOST, ZOOM_RELAY, 50000, 8801, ts=2.0),
    ]
    labels = derive_labels(make_manifest(), packets)
    assert [f.t_first for f in labels.flows] == [2.0, 9.0]


# --------------------------------------------------------------------------- #
# Producer -> consumer seam: a manifest built by the real VM4 merge code
# --------------------------------------------------------------------------- #

def test_manifest_from_real_producer_labels_noise_and_timeline():
    """Cross the seam decision 10 warns about: build the manifest through the real
    build_manifest (spec + heartbeats + capture), then label against it."""
    spec = Spec(
        session_id="sess-seam",
        meeting=Meeting(id="123456789", pwd="s3cret", zak="zak-token"),
        participant_count=2,
        roster=[
            RosterEntry(ip=HOST, zoom_role=schema.ROLE_HOST),
            RosterEntry(ip=JOINER2, zoom_role=schema.ROLE_JOINER),
            RosterEntry(ip=NOISE_VM, zoom_role=schema.ROLE_NONE,
                        noise=NoiseBlock(enabled=True, profile="iperf",
                                         target=IPERF_SERVER, ports="5201,5202")),
        ],
        turns=Turns(seed=4711, windows=[TurnWindow(0.0, 6.4, HOST)]),
        timing=Timing(preroll_s=3.0, duration_s=60.0, postroll_s=2.0,
                      join_delay_s={HOST: 0.0, JOINER2: 5.0}),
        seeds=Seeds(turns=4711, timing=9001),
    )
    heartbeats = [
        HeartbeatEvent(schema.EVENT_JOINED, HOST, 10.0),
        HeartbeatEvent(schema.EVENT_JOINED, JOINER2, 15.0),
        HeartbeatEvent(schema.EVENT_LEFT, HOST, 70.0),
        HeartbeatEvent(schema.EVENT_LEFT, JOINER2, 70.0),
    ]
    capture = Capture(t_start=0.0, t_stop=80.0, pcap_key="sessions/sess-seam/capture.pcap")
    manifest = Manifest.from_json(build_manifest(spec, heartbeats, capture).to_json())

    labels = derive_labels(manifest, [
        udp(NOISE_VM, IPERF_SERVER, 49152, 5201, ts=1.0),
        udp(HOST, ZOOM_RELAY, 50000, 8801, ts=12.0),
    ])
    assert [f.label for f in labels.flows] == [LABEL_NOISE, LABEL_ZOOM_MEDIA]
    assert [(w.t0, w.t1, w.party_count) for w in labels.timeline] == [
        (0.0, 10.0, 0), (10.0, 15.0, 1), (15.0, 70.0, 2), (70.0, 80.0, 0),
    ]
    assert labels.warnings == []


# --------------------------------------------------------------------------- #
# Output document + CLI (the read_pcap edge, against a real scapy-written file)
# --------------------------------------------------------------------------- #

def test_labels_document_shape_and_version():
    m = make_manifest([JoinLeave(ip=HOST, t_join=10.0, t_leave=90.0)])
    doc = json.loads(derive_labels(m, [udp(HOST, ZOOM_RELAY, 50000, 8801)]).to_json())
    assert doc["labeler_version"] == LABELER_VERSION
    assert doc["session_id"] == "sess-test"
    assert doc["timeline"][0].keys() == {"t0", "t1", "party_count", "ips"}
    assert doc["flows"][0].keys() == {"proto", "ip_a", "port_a", "ip_b", "port_b",
                                      "label", "rule", "packets", "bytes",
                                      "t_first", "t_last"}
    assert doc["warnings"] == []


def test_cli_reads_real_pcap_and_writes_labels_json(tmp_path, capsys):
    from scapy.layers.inet import IP, TCP, UDP
    from scapy.layers.l2 import Ether
    from scapy.utils import wrpcap

    pkts = [
        Ether() / IP(src=HOST, dst=ZOOM_RELAY) / UDP(sport=50000, dport=8801),
        Ether() / IP(src=ZOOM_RELAY, dst=HOST) / UDP(sport=8801, dport=50000),
        Ether() / IP(src=NOISE_VM, dst=IPERF_SERVER) / UDP(sport=49152, dport=5203),
        Ether() / IP(src=JOINER2, dst=ZOOM_RELAY) / TCP(sport=51000, dport=443),
    ]
    for i, p in enumerate(pkts):
        p.time = 10.0 + i
    wrpcap(str(tmp_path / "capture.pcap"), pkts)
    m = make_manifest(
        [JoinLeave(ip=HOST, t_join=10.0, t_leave=90.0),
         JoinLeave(ip=JOINER2, t_join=12.0, t_leave=90.0)],
    )
    (tmp_path / "manifest.json").write_text(m.to_json())

    assert main([str(tmp_path)]) == 0

    doc = json.loads((tmp_path / "labels.json").read_text())
    by_label = {f["label"] for f in doc["flows"]}
    assert by_label == {LABEL_ZOOM_MEDIA, LABEL_NOISE, LABEL_ZOOM_SIGNALING}
    media = next(f for f in doc["flows"] if f["label"] == LABEL_ZOOM_MEDIA)
    assert media["packets"] == 2  # both directions of the relay flow grouped
    assert "2-party" in capsys.readouterr().out