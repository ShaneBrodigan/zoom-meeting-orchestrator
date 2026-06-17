"""Generate a batch of labeled capture sessions on VM4.

This is the front door for *bulk* dataset generation — the step after the harness was
proven feature-complete (REFACTOR_DESIGN.md §7). It owns no orchestration logic of its
own: it reads the generation policy (``config/generation_plan.json``), asks
``generation_plan`` for a balanced list of per-session plans, and runs each one through
the already-verified ``SessionOrchestrator.run_session``. All the dataset-shaping (which
durations, which party sizes, which subnets host) lives in ``generation_plan`` and is
unit-tested there; this module is the thin live driver that wires it to real Zoom +
tshark + S3, like ``live_check_orchestrator.py`` but for a whole batch.

Run ON VM4 (the only host that can create the meeting, capture ens5, and reach S3 via
the instance role). Needs ``py-zoom-meeting-sdk/.env`` (Zoom S2S creds) and ``sudo`` for
the tshark capture + ``/tmp`` pcap (see live_check_orchestrator.py / launch-guide.md):

    sudo -E python3 -m orchestrator.bulk_generate --count 20 --seed 4711

``--seed`` is optional; omit it and a random master seed is chosen, printed, and recorded
into every manifest's ``timing_plan.run_seed`` so the batch is reproducible after the
fact. Noise is always on: VM5 runs ``client/noise.py`` independently, and every session's
roster records VM5's noise block (read from ``config/noise.json``).

Each session is one full call (5–30 min by default), so a batch of N takes roughly
N × (its duration) to run — start small.
"""

from __future__ import annotations

import argparse
import os
import secrets

from dotenv import load_dotenv

from common.infra import CLIENT_IPS, VM5_NOISE_IP
from common.s3 import SessionStore
from common.schema import ROLE_HOST, ROLE_JOINER, ROLE_NONE, NoiseBlock, RosterEntry
from orchestrator.generation_plan import SessionPlan, load_generation_plan
from orchestrator.session_orchestrator import SessionConfig, SessionOrchestrator

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PLAN_PATH = os.path.join(_REPO_ROOT, "config", "generation_plan.json")

# Load .env explicitly (robust under sudo, which can change cwd) — same as live_check.
load_dotenv(os.path.join(_REPO_ROOT, ".env"))


def build_roster(plan: SessionPlan, noise: NoiseBlock) -> list[RosterEntry]:
    """Turn one resolved session plan into a spec roster.

    The chosen participants become host (the one ``plan`` picked) + joiners; VM5 is always
    appended as a ``none`` VM carrying the recorded noise block (noise is always on)."""
    roster = [
        RosterEntry(ip=ip, zoom_role=ROLE_HOST if ip == plan.host_ip else ROLE_JOINER)
        for ip in plan.participant_ips
    ]
    roster.append(RosterEntry(ip=VM5_NOISE_IP, zoom_role=ROLE_NONE, noise=noise))
    return roster


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    seed = args.seed if args.seed is not None else secrets.randbelow(2**31)

    plan = load_generation_plan(args.plan)
    # VM5's noise recipe (config/noise.json in S3) — recorded into every roster so the
    # offline labeler can separate noise flows. VM4 only records it; it never starts noise.
    noise = SessionStore().read_noise_config().to_noise_block()
    session_plans = plan.build(args.count, seed, CLIENT_IPS)

    print(f"bulk_generate: {args.count} sessions, master seed = {seed}")
    print(f"  plan: {args.plan}")
    print("  (reproduce this batch with  --count %d --seed %d)\n" % (args.count, seed))

    orch = SessionOrchestrator.from_env()
    for i, sp in enumerate(session_plans, start=1):
        roster = build_roster(sp, noise)
        cfg = SessionConfig(
            roster=roster,
            seeds=sp.seeds,
            topic=args.topic,
            duration_range_s=sp.duration_range_s,
            duration_bucket_min=sp.duration_bucket_min,
            run_seed=seed,
        )
        lo, hi = sp.duration_range_s
        joiners = [ip for ip in sp.participant_ips if ip != sp.host_ip]
        print(f"[{i}/{args.count}] {sp.duration_bucket_min}-min bucket "
              f"(~{lo / 60:.1f}-{hi / 60:.1f} min) | host {sp.host_ip} | joiners {joiners}")
        manifest = orch.run_session(cfg)
        print(f"        -> {manifest.session_id}  "
              f"duration {manifest.capture.t_stop - manifest.capture.t_start:.0f}s wire\n")

    print(f"done: {args.count} sessions written to s3://.../sessions/")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m orchestrator.bulk_generate",
        description="Generate a balanced batch of labeled Zoom capture sessions on VM4.")
    p.add_argument("--count", type=int, required=True,
                   help="how many sessions to generate this batch")
    p.add_argument("--seed", type=int, default=None,
                   help="master seed (omit for a random one, which is printed + recorded)")
    p.add_argument("--plan", default=DEFAULT_PLAN_PATH,
                   help="path to the generation policy JSON (default: config/generation_plan.json)")
    p.add_argument("--topic", default="Bot Meeting", help="Zoom meeting topic")
    args = p.parse_args(argv)
    if args.count <= 0:
        p.error("--count must be positive")
    return args


if __name__ == "__main__":
    main()
