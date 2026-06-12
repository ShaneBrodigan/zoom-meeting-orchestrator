#!/usr/bin/env python3
"""Throwaway live driver for the client agent (Phase 3) — runs on VM1 / VM2.

NOT meant to be committed — it mirrors the live_check_orchestrator.py precedent. It is
the smallest possible driver that starts the real agent on a client VM: it polls S3 for a
new spec, matches this VM's private IP against the roster, and forks the real bot to join
the meeting. Pair it with live_check_orchestrator.py running on VM4.

Run ON a client VM (VM1 host / VM2 joiner), INSIDE the SDK container with host networking
so (a) the bot's Zoom media egresses from the VM's real subnet IP — which is what VM4's
pre-NAT capture labels on — and (b) IP auto-detection returns 10.0.1.x rather than the
container's address. The container needs:
  * the EC2 instance IAM role (S3 read/write) — automatic on the VM; boto3 picks it up,
  * boto3 + the Zoom SDK installed:   pip install boto3 zoom-meeting-sdk
  * the SDK JWT creds in the environment (ZOOM_APP_CLIENT_ID / ZOOM_APP_CLIENT_SECRET),
    e.g. from py-zoom-meeting-sdk/.env (these are the *bot* creds, NOT the VM4 ZOOM_S2S_*).

Typical launch (from the repo root inside the container):

    AGENT_IP=10.0.1.119  python live_check_agent.py      # on VM1 (host)
    AGENT_IP=10.0.2.67   python live_check_agent.py      # on VM2 (joiner)

AGENT_IP is optional with --network host (auto-detect works) but is the reliable belt-and-
braces: the roster is keyed on these exact IPs. Start this on BOTH client VMs first, THEN
run the orchestrator on VM4 — the agents prime past sessions on start-up and act only on
the spec that appears afterwards, so the order matters.

This runs forever (Ctrl-C to stop). On a successful run you'll see it pick up the session
and fork the bot; the join/leave then land in S3 at sessions/{id}/heartbeats/{ip}.json,
which VM4 folds into the manifest's joins_leaves.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from common.s3 import SessionStore
from client.agent import Agent, detect_private_ip, _fork_bot

# Load py-zoom-meeting-sdk/.env explicitly (robust even if cwd changes).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def main() -> None:
    store = SessionStore()
    ip = os.environ.get("AGENT_IP") or detect_private_ip()

    def launch(spec, entry, anchor, audio):
        print(f"[agent] new session {spec.session_id}: this VM ({entry.ip}) is "
              f"'{entry.zoom_role}' -> forking bot (anchor={anchor}, audio={audio})")
        return _fork_bot(store, spec, entry, anchor, audio)

    agent = Agent(store, ip, launch=launch)

    print(f"[agent] my private IP = {ip}")
    print("[agent] priming existing sessions (these will be skipped)...")
    agent.prime()
    print("[agent] polling S3 for a new spec... (start the orchestrator on VM4 now)\n")

    while True:
        agent.poll_once()
        agent._sleep(agent._poll_interval_s)


if __name__ == "__main__":
    main()
