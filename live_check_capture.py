"""Throwaway live check for orchestrator/capture.py (step 2f) on VM4.

Run on VM4 with capture privileges (sudo). It:
  1. starts the real tshark via PacketCapture (proves the generated pre-NAT
     filter is accepted and that start() reaches tshark's "Capturing on" line),
  2. waits while you generate cross-subnet traffic from VM1 (e.g. ping 8.8.8.8),
  3. stops the capture, then reads capture.pcap back and checks decision 2:
       - real client IP 10.0.1.119 IS present (pre-NAT copy kept),
       - post-NAT twin 10.0.0.7 is ABSENT (collapsed copy dropped),
       - SSH (tcp port 22) is ABSENT (VM4 control traffic excluded).

Not committed. Delete ~/zoomcheck after the check.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator.capture import PacketCapture, CaptureError  # noqa: E402

# dumpcap drops privileges before writing the pcap, so the file must live where
# that unprivileged writer can create it: /tmp (mode 1777), NOT a home dir (755).
PCAP = "/tmp/zoom_capture_check.pcap"
CLIENT_IP = "10.0.1.119"   # VM1 pre-NAT source — MUST appear
POSTNAT_IP = "10.0.0.7"    # VM4 MASQUERADE twin — MUST be dropped


def _read_count(display_filter: str | None) -> int:
    """Count packets in the pcap, optionally matching a display filter."""
    argv = ["tshark", "-r", PCAP, "-n"]
    if display_filter:
        argv += ["-Y", display_filter]
    out = subprocess.run(argv, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"tshark readback failed: {out.stderr.strip()}")
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def main() -> int:
    cap = PacketCapture(PCAP)
    print(f"capture filter : {cap.capture_filter}")
    print(f"argv           : {' '.join(cap.build_argv())}\n")

    try:
        t_start = cap.start()
    except CaptureError as err:
        print(f"FAIL: tshark did not start capturing -> {err}")
        return 1
    print(f"OK  : tshark is capturing (t_start={t_start:.3f}).")
    print("\n>>> Now, in another PuTTY window: ssh vm1, then run:  ping -c 20 8.8.8.8")
    input(">>> When the ping has finished, press Enter here to stop the capture...\n")

    t_stop = cap.stop()
    print(f"OK  : capture stopped (t_stop={t_stop:.3f}, window={t_stop - t_start:.1f}s).\n")

    if not os.path.exists(PCAP) or os.path.getsize(PCAP) == 0:
        print(f"FAIL: {PCAP} is missing or empty.")
        return 1

    total = _read_count(None)
    client = _read_count(f"ip.addr == {CLIENT_IP}")
    postnat = _read_count(f"ip.addr == {POSTNAT_IP}")
    ssh = _read_count("tcp.port == 22")

    print(f"pcap size      : {os.path.getsize(PCAP)} bytes, {total} packets")
    print(f"client {CLIENT_IP}: {client} packets  (expect > 0  — pre-NAT copy kept)")
    print(f"post-NAT {POSTNAT_IP} : {postnat} packets  (expect 0    — twin dropped)")
    print(f"ssh tcp/22     : {ssh} packets  (expect 0    — SSH excluded)\n")

    ok = total > 0 and client > 0 and postnat == 0 and ssh == 0
    print("RESULT: " + ("PASS — decision 2 filter behaves as designed."
                        if ok else "FAIL — see counts above."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())