#!/usr/bin/env python3
"""Throwaway live PLUMBING check for orchestrator/session_orchestrator.py (2g).

NOT meant to be committed — it mirrors the deleted live_check_scheduler.py /
live_check_capture.py precedent. It is the smallest possible driver that actually
invokes the conductor against real Zoom + real tshark + real S3, so we can confirm
the meeting/capture/spec/manifest plumbing end to end before the bots exist.

Run ON VM4 (it is the only host that can create the meeting, capture ens5, and reach
S3 via the instance role). Needs:
  * py-zoom-meeting-sdk/.env present with the Zoom S2S creds (ZOOM_S2S_*),
  * the EC2 instance IAM role (S3 read/write) — automatic on VM4,
  * sudo, because tshark captures on ens5 AND the /tmp pcap is owned by the
    privilege-dropped user, so the upload step must read it with privilege.

From inside py-zoom-meeting-sdk on VM4 (adjust the interpreter path to VM4's venv):

    sudo -E python3 live_check_orchestrator.py

PLUMBING-ONLY EXPECTATION: no bots join, so the manifest's joins_leaves come back
all None and the pcap is (near-)empty of media. That is the CORRECT result — it
proves the conductor's plumbing, not a multi-party call.

Cleanup afterwards (sticky /tmp — plain rm fails):
    sudo rm -f /tmp/sess-*.pcap
    pgrep -a tshark; pgrep -a dumpcap        # confirm nothing left running
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from common.schema import ROLE_HOST, ROLE_JOINER, ROLE_NONE, RosterEntry, Seeds
from orchestrator.session_orchestrator import SessionConfig, SessionOrchestrator
from orchestrator.timing import generate_timing

# Load py-zoom-meeting-sdk/.env explicitly (robust under sudo, which can change cwd).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def main() -> None:
    # 2-party roster from the AWS setup doc: VM1 host (private1), VM2 joiner (private2).
    config = SessionConfig(
        roster=[
            RosterEntry(ip="10.0.1.119", zoom_role=ROLE_HOST),    # VM1
            RosterEntry(ip="10.0.2.67", zoom_role=ROLE_JOINER),   # VM2
            RosterEntry(ip="10.0.3.53", zoom_role=ROLE_JOINER),   # VM3
        ],
        seeds=Seeds(turns=4711, timing=9001),
    )

    # Preview the seeded timing so the wait is no surprise (run_session computes the
    # exact same values from the same seed — generate_timing is deterministic).
    joining = [e.ip for e in config.roster if e.zoom_role != ROLE_NONE]
    t = generate_timing(config.seeds.timing, joining)
    print(f"session_id = {config.session_id}")
    print(f"timing     = preroll {t.preroll_s:.1f}s | duration {t.duration_s:.1f}s "
          f"| postroll {t.postroll_s:.1f}s  (~{t.preroll_s + t.duration_s + t.postroll_s:.0f}s total)")
    print("running... (no bots will join — empty joins are the expected plumbing result)\n")

    orch = SessionOrchestrator.from_env()
    manifest = orch.run_session(config)

    print("\n--- manifest (raw facts) ---")
    print(f"session_id : {manifest.session_id}")
    print(f"meeting_id : {manifest.meeting_id}")
    print(f"capture    : t_start={manifest.capture.t_start} t_stop={manifest.capture.t_stop}")
    print(f"             pcap_key={manifest.capture.pcap_key}")
    print(f"joins_leaves (expect all None):")
    for jl in manifest.joins_leaves:
        print(f"   {jl.ip}: t_join={jl.t_join} t_leave={jl.t_leave}")

    # Redaction self-check: credentials must never reach the manifest.
    blob = manifest.to_json()
    assert "pwd" not in blob and "zak" not in blob, "REDACTION FAILURE: credential in manifest!"
    print("\nredaction OK: no pwd/zak in manifest")
    print("S3: sessions/%s/{spec.json, manifest.json, capture.pcap}" % manifest.session_id)
    print("\nREMINDER: clean up with  sudo rm -f /tmp/%s.pcap" % manifest.session_id)


if __name__ == "__main__":
    main()