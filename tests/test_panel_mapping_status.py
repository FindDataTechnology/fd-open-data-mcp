"""Source detail: landed is not mapped, and unknown is not unmapped.

The page already says "we ingested this source" (the crawler fact). It must also
say whether anyone knows which part of the ontology it fills — and when it cannot
tell, it must say so rather than showing "not mapped".
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from fd_open_data_mcp.panel import mapping_status as ms  # noqa: E402


def cw(**over):
    row = {"relation": "same_as",
           "peer": {"kind": "external_id", "system": "src-a", "id": "src-a"},
           "confidence": 0.95}
    row.update(over)
    return row


def test_no_reader_reports_unknown_not_unmapped():
    """'We never checked' must not read as 'we checked and there is nothing'."""
    out = ms.status_for("src-a", reader=None)
    assert out["state"] == "unknown"


def test_unreachable_reader_reports_unknown():
    def boom(_):
        raise ConnectionError("down")
    out = ms.status_for("src-a", reader=boom)
    assert out["state"] == "unknown"
    assert "ConnectionError" in out["reason"]


def test_malformed_payload_reports_unknown():
    assert ms.status_for("a", reader=lambda s: ["not", "a", "dict"])["state"] == "unknown"
    assert ms.status_for("a", reader=lambda s: {})["state"] == "unknown"


def test_confirmed_crosswalk_is_mapped():
    out = ms.status_for("src-a", reader=lambda s: {"crosswalks": [cw()]})
    assert out["state"] == "mapped"
    assert out["evaluable"] is False


def test_complete_connectivity_is_evaluable():
    c = {s: {"ok": True, "evidence": f"p-{s}"} for s in ms.CONNECTIVITY_STATES}
    out = ms.status_for("src-a", reader=lambda s: {"crosswalks": [cw(connectivity=c)]})
    assert out["state"] == "mapped" and out["evaluable"] is True


def test_empty_list_is_unmapped_not_unknown():
    """We looked and found nothing — that is a checked absence."""
    out = ms.status_for("src-a", reader=lambda s: {"crosswalks": []})
    assert out["state"] == "unmapped"


def test_low_confidence_is_not_mapped():
    out = ms.status_for("src-a", reader=lambda s: {"crosswalks": [cw(confidence=0.3)]})
    assert out["state"] == "unmapped"
    assert out["pending"] == 1


def test_crosswalk_for_another_system_is_ignored():
    other = cw(peer={"kind": "external_id", "system": "src-b", "id": "src-b"})
    out = ms.status_for("src-a", reader=lambda s: {"crosswalks": [other]})
    assert out["state"] == "unmapped"


# ------------------------------------------------------------------- badges


def test_badges_distinguish_three_states():
    labels = {ms.badge({"state": s})["label"] for s in ("mapped", "unmapped", "unknown")}
    assert len(labels) == 3, "three states must read as three different things"


def test_unknown_badge_carries_the_reason():
    b = ms.badge({"state": "unknown", "reason": "读面不可达：ConnectionError"})
    assert "ConnectionError" in b["detail"]
