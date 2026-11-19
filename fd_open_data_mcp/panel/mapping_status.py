"""Mapping status for a source, read through an injected reader.

The source detail page already shows a **landed** badge (the crawler-side fact:
this source has a row in `crawl_sources`). It answers "did we ingest it?". It
does not answer "do we know which part of the ontology it fills?" — and those
are different states with different remedies.

The ontology lives in a **different database** (the platform's), so this module
never queries it directly. The caller injects a `reader(source) -> dict` and the
panel stays free of a second database driver and of a hard dependency on the
platform being reachable.

Three states, deliberately not two:

    mapped      a confirmed crosswalk exists for this source
    unmapped    we looked; there is none
    unknown     we could not look

`unknown` must not collapse into `unmapped`: "we never checked" and "we checked
and there is nothing" send an operator to different places.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

#: Reviewer floor for "this mapping has been looked at". Matches the seam's
#: `crosswalk.CROSSWALK_CONFIDENCE_THRESHOLD`; a mapping below it is pending
#: review, which is not the same as mapped.
CONFIDENCE_FLOOR = 0.8

#: The three connectivity states. All three cleared = the source is measurable.
CONNECTIVITY_STATES = ("catalog", "executor_tests", "live")


def status_for(
    source: str,
    *,
    reader: Optional[Callable[[str], dict]] = None,
) -> dict:
    """Mapping status for one source.

    ``reader`` returns the platform's answer for that source, shaped like the
    bench read face: ``{"crosswalks": [{relation, peer, confidence, connectivity}]}``.
    A ``None`` reader (or one that raises) yields ``state="unknown"`` — never an
    empty "unmapped", which would look like a verified absence.
    """
    if reader is None:
        return {"state": "unknown", "reason": "未配置本体读面"}

    try:
        payload = reader(source)
    except Exception as exc:  # noqa: BLE001 - unreachable is a state, not a crash
        return {"state": "unknown", "reason": f"读面不可达：{type(exc).__name__}"}

    if not isinstance(payload, dict):
        return {"state": "unknown", "reason": "读面返回非对象"}

    rows = payload.get("crosswalks")
    if not isinstance(rows, list):
        return {"state": "unknown", "reason": "读面返回缺 crosswalks"}

    confirmed = []
    for row in rows:
        peer = (row or {}).get("peer") or {}
        system = peer.get("system") if isinstance(peer, dict) else None
        if system not in (None, source):
            continue
        confidence = (row or {}).get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) \
                and float(confidence) >= CONFIDENCE_FLOOR:
            confirmed.append(row)

    if not confirmed:
        return {"state": "unmapped", "reason": "无已确认的映射",
                "pending": len(rows)}

    evaluable = any(_complete((r or {}).get("connectivity")) for r in confirmed)
    return {
        "state": "mapped",
        "evaluable": evaluable,
        "confirmed": len(confirmed),
        "pending": len(rows) - len(confirmed),
    }


def badge(status: dict) -> dict:
    """The label and tone the template renders.

    Kept separate from `status_for` so the wording has one home: two places
    phrasing "mapped / not mapped / unknown" would drift, and the third one is
    the one people misread.
    """
    state = status.get("state")
    if state == "mapped":
        extra = "（可评）" if status.get("evaluable") else "（映射已确认，三态不全）"
        return {"label": f"已映射{extra}", "tone": "success",
                "detail": f"确认 {status.get('confirmed', 0)} 条，待复核 {status.get('pending', 0)} 条"}
    if state == "unmapped":
        return {"label": "未映射", "tone": "warn",
                "detail": f"已查看，无已确认映射（待复核 {status.get('pending', 0)} 条）"}
    return {"label": "映射状态未知", "tone": "muted",
            "detail": status.get("reason") or "未读面"}


def _complete(connectivity: Any) -> bool:
    if not isinstance(connectivity, dict):
        return False
    for state in CONNECTIVITY_STATES:
        value = connectivity.get(state)
        if not isinstance(value, dict) or value.get("ok") is not True:
            return False
        if not value.get("evidence"):
            return False
    return True
