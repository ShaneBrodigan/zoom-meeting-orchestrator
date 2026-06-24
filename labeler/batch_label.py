"""Label a whole dataset at once, locally, with a pass/FLAG QC summary.

The single-session labeler (``labeler.derive_labels``) is the engine; this is the bulk
front door you run on your own machine after a generation batch. It:

1. pulls every session from S3 to a local folder (incremental ``aws s3 sync`` — only new
   sessions transfer), unless ``--no-pull``;
2. labels each session that has a ``manifest.json`` + ``capture.pcap``, skipping ones
   already labeled (so re-runs are cheap) unless ``--force``;
3. writes ``labels.json`` next to each session and uploads it back beside the capture in
   S3 (so the dataset and its answer key stay together), unless ``--no-push``;
4. runs cheap quality checks on each result and prints one ``OK``/``FLAG`` line per
   session, then a summary of every flag at the end — it never deletes anything.

A session is FLAGged (not trusted) when any of these is true:
  * the labeler emitted warnings;
  * the call never reached the party count the roster expected (e.g. a 3-bot session
    whose timeline tops out at 2 — a bot that silently never joined);
  * no ``zoom_media`` flows were found at all (a capture with no call media in it).

Run from inside ``py-zoom-meeting-sdk`` (so ``common``/``labeler`` import), with the AWS
CLI configured and scapy installed:

    python -m labeler.batch_label                 # pull, label, push, summarize
    python -m labeler.batch_label --no-push       # keep labels local only
    python -m labeler.batch_label --no-pull       # label what's already downloaded
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

from common.schema import Manifest, ROLE_NONE
from labeler.derive_labels import (
    LABEL_ZOOM_MEDIA,
    derive_labels,
    read_pcap,
)

DEFAULT_BUCKET = "zoom-bot-dataset-s3"
DEFAULT_LOCAL_DIR = "sessions"


@dataclass
class SessionQC:
    """The outcome of labeling one session."""
    session_id: str
    ok: bool
    reasons: list[str]          # why it was flagged (empty when ok)
    expected_parties: int
    max_parties: int
    flow_counts: dict[str, int]

    def line(self) -> str:
        status = "OK  " if self.ok else "FLAG"
        flows = ", ".join(f"{k}:{v}" for k, v in sorted(self.flow_counts.items()))
        note = "" if self.ok else "  <- " + "; ".join(self.reasons)
        return (f"{status} {self.session_id}  "
                f"parties {self.max_parties}/{self.expected_parties}  [{flows}]{note}")


def label_session(session_dir: Path, *, force: bool = False) -> SessionQC | None:
    """Label one downloaded session folder and quality-check the result.

    Returns the ``SessionQC``, or ``None`` if the folder isn't a complete session
    (missing manifest or pcap) or was already labeled and ``force`` is False.
    Writes/overwrites ``labels.json`` in the folder when it labels.
    """
    manifest_path = session_dir / "manifest.json"
    pcap_path = session_dir / "capture.pcap"
    labels_path = session_dir / "labels.json"
    if not manifest_path.exists() or not pcap_path.exists():
        return None
    if labels_path.exists() and not force and \
            labels_path.stat().st_mtime >= pcap_path.stat().st_mtime:
        return None

    manifest = Manifest.from_json(manifest_path.read_text())
    labels = derive_labels(manifest, read_pcap(pcap_path))
    labels_path.write_text(labels.to_json())
    return _qc(manifest, labels)


def _qc(manifest: Manifest, labels) -> SessionQC:
    """Apply the pass/FLAG rules to a freshly derived label set."""
    expected = sum(1 for e in manifest.roster if e.zoom_role != ROLE_NONE)
    max_parties = max((w.party_count for w in labels.timeline), default=0)
    flow_counts: dict[str, int] = {}
    for f in labels.flows:
        flow_counts[f.label] = flow_counts.get(f.label, 0) + 1

    reasons: list[str] = []
    if labels.warnings:
        reasons.append(f"{len(labels.warnings)} warning(s): {'; '.join(labels.warnings)}")
    if max_parties != expected:
        reasons.append(f"timeline reached {max_parties}-party, expected {expected}")
    if flow_counts.get(LABEL_ZOOM_MEDIA, 0) == 0:
        reasons.append("no zoom_media flows")

    return SessionQC(
        session_id=manifest.session_id,
        ok=not reasons,
        reasons=reasons,
        expected_parties=expected,
        max_parties=max_parties,
        flow_counts=flow_counts,
    )


def _sync_down(bucket: str, local_dir: Path) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["aws", "s3", "sync", f"s3://{bucket}/sessions/", str(local_dir)],
        check=True,
    )


def _push_labels(bucket: str, session_id: str, labels_path: Path) -> None:
    subprocess.run(
        ["aws", "s3", "cp", str(labels_path),
         f"s3://{bucket}/sessions/{session_id}/labels.json"],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    local_dir = Path(args.local_dir)

    if not args.no_pull:
        print(f"syncing s3://{args.bucket}/sessions/ -> {local_dir}/ ...")
        _sync_down(args.bucket, local_dir)

    results: list[SessionQC] = []
    for session_dir in sorted(p for p in local_dir.iterdir() if p.is_dir()):
        qc = label_session(session_dir, force=args.force)
        if qc is None:
            continue
        print(qc.line())
        results.append(qc)
        if not args.no_push:
            _push_labels(args.bucket, qc.session_id, session_dir / "labels.json")

    flagged = [r for r in results if not r.ok]
    print(f"\nlabeled {len(results)} session(s); {len(flagged)} flagged.")
    for r in flagged:
        print("  " + r.line())
    return 1 if flagged else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m labeler.batch_label",
        description="Pull, label, and QC a whole session dataset locally.")
    p.add_argument("--bucket", default=DEFAULT_BUCKET, help="S3 bucket (default: %(default)s)")
    p.add_argument("--local-dir", default=DEFAULT_LOCAL_DIR,
                   help="local folder to sync into / label (default: %(default)s)")
    p.add_argument("--no-pull", action="store_true", help="skip the S3 sync; label local folders")
    p.add_argument("--no-push", action="store_true", help="keep labels.json local; don't upload")
    p.add_argument("--force", action="store_true", help="relabel even if labels.json is current")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())