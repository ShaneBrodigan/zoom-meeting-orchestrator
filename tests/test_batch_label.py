"""Checkpoint tests for the batch labeler's QC rules (labeler/batch_label.py).

The S3 sync/push and the labeling engine are exercised elsewhere; here we pin the pure
pass/FLAG decision in ``_qc`` so a bad session can't slip through a big batch unnoticed.
Manifest/Labels are duck-typed with SimpleNamespace — _qc only reads roster, timeline,
flows, and warnings.

Run with:  pytest tests/test_batch_label.py
"""

from types import SimpleNamespace

from common.schema import ROLE_HOST, ROLE_JOINER, ROLE_NONE
from labeler.batch_label import _qc


def _roster(*roles):
    return [SimpleNamespace(ip=f"10.0.0.{i}", zoom_role=r) for i, r in enumerate(roles)]


def _manifest(roster):
    return SimpleNamespace(session_id="sess-test", roster=roster)


def _labels(party_counts, flow_labels, warnings=()):
    timeline = [SimpleNamespace(party_count=p) for p in party_counts]
    flows = [SimpleNamespace(label=l) for l in flow_labels]
    return SimpleNamespace(timeline=timeline, flows=flows, warnings=list(warnings))


# A healthy 3-party session: ramped to 3, has media, noise on VM5, no warnings.
HEALTHY_ROSTER = _roster(ROLE_HOST, ROLE_JOINER, ROLE_JOINER, ROLE_NONE)
HEALTHY_FLOWS = ["zoom_media", "zoom_media", "zoom_signaling", "noise", "other"]


def test_healthy_session_passes():
    qc = _qc(_manifest(HEALTHY_ROSTER), _labels([0, 1, 2, 3, 2, 0], HEALTHY_FLOWS))
    assert qc.ok
    assert qc.reasons == []
    assert qc.expected_parties == 3   # VM5 (role none) is not counted
    assert qc.max_parties == 3
    assert qc.flow_counts["zoom_media"] == 2


def test_warnings_flag():
    qc = _qc(_manifest(HEALTHY_ROSTER),
             _labels([0, 1, 2, 3], HEALTHY_FLOWS, warnings=["unmapped flow"]))
    assert not qc.ok
    assert any("warning" in r for r in qc.reasons)


def test_party_shortfall_flags_a_silent_bot():
    # 3 bots rostered but the call never got past 2 -> one bot never really joined.
    qc = _qc(_manifest(HEALTHY_ROSTER), _labels([0, 1, 2, 1, 0], HEALTHY_FLOWS))
    assert not qc.ok
    assert any("2-party" in r and "expected 3" in r for r in qc.reasons)


def test_no_media_flags():
    qc = _qc(_manifest(HEALTHY_ROSTER),
             _labels([0, 1, 2, 3], ["zoom_signaling", "noise", "other"]))
    assert not qc.ok
    assert any("zoom_media" in r for r in qc.reasons)


def test_two_party_session_expects_two():
    roster = _roster(ROLE_HOST, ROLE_JOINER, ROLE_NONE)
    qc = _qc(_manifest(roster), _labels([0, 1, 2, 1, 0], HEALTHY_FLOWS))
    assert qc.ok
    assert qc.expected_parties == 2