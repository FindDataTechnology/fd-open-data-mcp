"""Column->concept binding management: propose, threshold-gate, confirm, review.

Propose-and-confirm policy (design.md D4):
  - Bindings carry ``confidence`` + ``provenance`` (llm / manual / sample-confirmed).
  - Bindings below the confidence threshold are retained in a review queue and
    excluded from dispatch.
  - A real fetch that returns a matching column promotes a binding to
    ``sample-confirmed`` (design.md D4, spec semantic-layer).
  - Two columns are never silently merged; each keeps its own binding.

The gate was previously satisfiable by confidence alone. The rule table emits
0.85-0.9, so every machine proposal cleared it and the review step gated
nothing — concept `price.close`/`stock` reached 117 dispatch-eligible bindings.
A machine-proposed binding now needs confirmation, with confidence as a floor
rather than a substitute. That is a visible narrowing of live candidates, so it
ships behind ``FD_BINDING_ELIGIBILITY_GATE`` defaulting to ``report``:
:func:`dispatch_candidates` keeps its current behavior, and
:func:`eligibility_report` says what enforcing would withhold.
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn,
)
from fd_open_data_mcp.semantic.mapper_llm import (
    DEFAULT_THRESHOLD, domain_conflict, function_domain, propose_concept,
)

# Provenance recorded when a binding is confirmed by rule rather than by a human
# or by a real-fetch sample (design D7). Kept distinct so a machine confirmation
# is never mistaken for evidence a person produced.
CONFIRMED_BY_RULE = "rule-confirmed"

def verify_functions_by_rule(session: Session, dry_run: bool = True) -> dict:
    """Mark as verified the functions whose support is machine-checkable.

    ``fetch/dispatch.py::_bindings_for_source`` filters ``fn.verified``, so a
    binding is dispatchable only when the BINDING is eligible *and* its FUNCTION
    is verified. That second gate is what leaves the A-share price path dead:
    concept ``price.close``/``stock`` has 40 ``stock_*`` functions bound, all
    ``verified = false``, so the only candidates it can ever dispatch are the
    cross-domain bindings that caused the incident.

    Evidence required — both, so the rule refuses rather than assumes:

      * **domain consistency**: the function's declared domain matches the
        ``entity_type`` of EVERY concept it is bound to. A fund or options
        endpoint bound to a stock concept is therefore never verified.
      * **resolvability**: the function's endpoint resolves in this environment.

    A function that declares no domain supplies no positive evidence and is left
    alone, as is one with no bindings.

    Scope, stated plainly: this makes a function *eligible* for dispatch. It does
    NOT assert the endpoint returns data from a given cluster's egress — that is
    route health, which is what demotion is for.

    ``dry_run`` (the default) reports without writing.
    """
    from fd_open_data_mcp.fetch.capability import RESOLVABLE, check_command

    rows = session.execute(
        text(
            "SELECT DISTINCT f.id, f.command, s.name, c.entity_type "
            "FROM concept_bindings b "
            "JOIN columns   col ON col.id = b.column_id "
            "JOIN functions f   ON f.id = col.function_id "
            "JOIN sources   s   ON s.id = f.source_id "
            "JOIN concepts  c   ON c.id = b.concept_id "
            "WHERE f.verified = :unverified"
        ),
        {"unverified": False},
    ).fetchall()

    by_function: dict[int, dict] = {}
    for function_id, command, source, entity_type in rows:
        entry = by_function.setdefault(
            function_id, {"command": command, "source": source,
                          "entity_types": set()})
        entry["entity_types"].add(entity_type)

    counts = {"considered": 0, "verifiable": 0, "no_declared_domain": 0,
              "domain_inconsistent": 0, "unresolvable": 0, "unverifiable": 0}
    verifiable: list[dict] = []
    capability_cache: dict[tuple[str, str], str] = {}

    for function_id, entry in sorted(by_function.items()):
        counts["considered"] += 1
        declared = function_domain(entry["command"], entry["source"])
        if declared is None:
            counts["no_declared_domain"] += 1
            continue
        if entry["entity_types"] != {declared}:
            # bound to something outside its declared domain -> refuse
            counts["domain_inconsistent"] += 1
            continue

        key = (entry["source"], entry["command"])
        if key not in capability_cache:
            capability_cache[key] = check_command(*key)[0]
        status = capability_cache[key]
        if status != RESOLVABLE:
            counts["unverifiable" if status == "unverifiable" else "unresolvable"] += 1
            continue

        counts["verifiable"] += 1
        verifiable.append({"function_id": function_id,
                           "source": entry["source"],
                           "command": entry["command"],
                           "declared_domain": declared})

    counts["dry_run"] = dry_run
    counts["functions"] = verifiable

    if not dry_run and verifiable:
        ids = [v["function_id"] for v in verifiable]
        for start in range(0, len(ids), 500):
            session.query(Function).filter(
                Function.id.in_(ids[start:start + 500])
            ).update({"verified": True}, synchronize_session=False)
        session.commit()
    return counts


def _concept_codes(session: Session, concept_ids: list[int]) -> dict[int, str]:
    """``concept_id -> code``, minimal projection (see _concept_entity_types)."""
    out: dict[int, str] = {}
    for cid in concept_ids or []:
        row = session.execute(
            text("SELECT code FROM concepts WHERE id = :cid"), {"cid": cid}
        ).fetchone()
        if row is not None:
            out[cid] = row[0]
    return out


def _concept_entity_types(session: Session, concept_ids: list[int]) -> dict[int, str]:
    """``concept_id -> entity_type``, read with a minimal projection.

    Deliberately NOT an ORM ``Concept`` load. The deployed canonical store is
    missing ``concepts.concept_code``, so selecting the whole row fails there
    for a field this query does not need — and the confirmation pass must run
    against that store. Cached per call: a plan's unconfirmed bindings share a
    handful of concepts.
    """
    out: dict[int, str] = {}
    for cid in set(concept_ids):
        if cid is None:
            continue
        row = session.execute(
            text("SELECT entity_type FROM concepts WHERE id = :cid"), {"cid": cid}
        ).fetchone()
        if row is not None:
            out[cid] = row[0]
    return out


# Rollout switch for the tightened eligibility gate. "report" (default) keeps the
# pre-change behavior so a mis-written gate cannot silently narrow a live crawl;
# "enforce" applies it.
ELIGIBILITY_GATE_ENV = "FD_BINDING_ELIGIBILITY_GATE"
_MODE_REPORT = "report"
_MODE_ENFORCE = "enforce"


def gate_mode() -> str:
    """The active gate mode, ``report`` or ``enforce``."""
    raw = (os.environ.get(ELIGIBILITY_GATE_ENV) or _MODE_REPORT).strip().lower()
    return _MODE_ENFORCE if raw == _MODE_ENFORCE else _MODE_REPORT


def _legacy_eligible(binding: ConceptBinding, threshold: float) -> bool:
    """The pre-change predicate: confirmation, OR confidence at/above threshold."""
    return (binding.provenance in ("manual", "sample-confirmed")
            or binding.confidence >= threshold)


def _gated_eligible(binding: ConceptBinding, threshold: float) -> bool:
    """The tightened predicate: a machine-proposed binding must be confirmed.

    Confidence stays a floor — a low-confidence confirmed binding is still
    withheld — but it is no longer sufficient on its own.
    """
    if binding.provenance == "llm" and not binding.reviewed:
        return False
    return _legacy_eligible(binding, threshold)


def _get_or_create_concept(
    session: Session, code: str, entity_type: str, measure: str, unit: str, frequency: str,
) -> Concept:
    c = session.query(Concept).filter_by(
        code=code, entity_type=entity_type, measure=measure or "", unit=unit or "", frequency=frequency,
    ).first()
    if c is None:
        c = Concept(
            code=code, entity_type=entity_type, measure=measure or "",
            unit=unit or "", frequency=frequency or "unknown", verified=False,
        )
        session.add(c)
        session.flush()
    return c


def propose_bindings(session: Session, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Walk all columns and propose a concept for each via the mapper.

    Existing confirmed/manual/sample-confirmed bindings are not overwritten.
    Returns counts of proposals and how many fell below the threshold.
    """
    proposed = 0
    refused_domain = 0
    below = 0
    for col in session.query(FunctionColumn).all():
        fn = col.function
        command = fn.command if fn is not None else None
        source = (fn.source.name if fn is not None and fn.source is not None
                  else None)
        prop = propose_concept(col.name, col.description, col.semantic_type,
                               command=command, source=source)
        if not prop:
            # A refusal is a cross-domain proposal (see mapper_llm.function_domain);
            # counted so the narrowing is visible rather than silent.
            if domain_conflict(col.name, col.semantic_type, command, source) is not None:
                refused_domain += 1
            continue
        concept = _get_or_create_concept(
            session, prop["code"], prop["entity_type"], prop["measure"], prop["unit"], prop["frequency"],
        )
        existing = session.query(ConceptBinding).filter_by(
            concept_id=concept.id, column_id=col.id,
        ).first()
        if existing is not None:
            continue
        session.add(ConceptBinding(
            concept_id=concept.id, column_id=col.id,
            confidence=prop["confidence"], provenance="llm", reviewed=False,
        ))
        proposed += 1
        if prop["confidence"] < threshold:
            below += 1
    session.commit()
    return {"proposed": proposed, "below_threshold": below,
            "refused_domain_mismatch": refused_domain}


def dispatch_candidates(
    session: Session, concept_id: int, threshold: float = DEFAULT_THRESHOLD,
) -> list[ConceptBinding]:
    """Bindings eligible for dispatch for a concept.

    Report-only by default: the eligible set is the pre-change set, so the gate
    cannot narrow a live crawl before it has been reviewed. In ``enforce`` mode
    the tightened predicate applies (a machine-proposed binding needs
    confirmation). :func:`eligibility_report` states the difference.
    """
    rows = session.query(ConceptBinding).filter_by(concept_id=concept_id).all()
    predicate = (_gated_eligible if gate_mode() == _MODE_ENFORCE
                 else _legacy_eligible)
    return [b for b in rows if predicate(b, threshold)]


def review_queue(session: Session, threshold: float = DEFAULT_THRESHOLD) -> list[ConceptBinding]:
    """Machine-proposed bindings awaiting confirmation.

    Under the tightened gate every unconfirmed machine proposal needs review,
    however confident it is; before that, only those below the threshold did.
    """
    rows = session.query(ConceptBinding).filter_by(reviewed=False, provenance="llm").all()
    if gate_mode() == _MODE_ENFORCE:
        return list(rows)
    return [b for b in rows if b.confidence < threshold]


def eligibility_report(session: Session, concept_ids: Optional[list[int]] = None) -> dict:
    """What the tightened gate would withhold, and the per-concept delta.

    Read-only: nothing is excluded, disabled or written. ``concept_ids`` limits
    the report; omit it for every concept that has bindings.
    """
    query = session.query(ConceptBinding.concept_id).distinct()
    if concept_ids is not None:
        query = query.filter(ConceptBinding.concept_id.in_(list(concept_ids)))
    ids = [cid for (cid,) in query.all()]

    withheld: list[dict] = []
    before: dict[int, int] = {}
    after: dict[int, int] = {}
    for cid in ids:
        rows = session.query(ConceptBinding).filter_by(concept_id=cid).all()
        legacy = [b for b in rows if _legacy_eligible(b, DEFAULT_THRESHOLD)]
        gated = [b for b in rows if _gated_eligible(b, DEFAULT_THRESHOLD)]
        before[cid] = len(legacy)
        after[cid] = len(gated)
        keep = {b.id for b in gated}
        for b in legacy:
            if b.id in keep:
                continue
            col = session.get(FunctionColumn, b.column_id)
            withheld.append({
                "binding_id": b.id, "concept_id": cid,
                "function_id": col.function_id if col else None,
                "command": (col.function.command
                            if col is not None and col.function is not None else None),
                "column": col.name if col else None,
                "provenance": b.provenance, "reviewed": b.reviewed,
                "confidence": b.confidence,
            })

    concept_codes = _concept_codes(session, ids)
    for row in withheld:
        row["concept_code"] = concept_codes.get(row["concept_id"])

    losing = [cid for cid in ids if after[cid] < before[cid]]
    emptied = [cid for cid in ids if before[cid] > 0 and after[cid] == 0]
    return {
        "gate_mode": gate_mode(),
        "concepts_checked": len(ids),
        "bindings_withheld": len(withheld),
        "withheld": withheld,
        "candidates_before": before,
        "candidates_after": after,
        "concepts_losing_candidates": sorted(losing),
        "concepts_left_with_none": sorted(emptied),
    }


def confirm_binding(
    session: Session, binding_id: int, provenance: str = "manual",
) -> Optional[ConceptBinding]:
    """Mark a binding reviewed and promote its provenance/confidence."""
    b = session.get(ConceptBinding, binding_id)
    if b is None:
        return None
    b.reviewed = True
    b.provenance = provenance
    b.confidence = max(b.confidence, DEFAULT_THRESHOLD)
    session.commit()
    return b


def promote_on_sample(
    session: Session, function_id: int, returned_columns: list[tuple[str, str]],
) -> int:
    """Promote bindings whose function returned a matching column (name [+ type]).

    ``returned_columns`` is a list of (name, type) from a real fetch. A matching
    name promotes the binding to ``sample-confirmed`` (design.md D4).
    """
    count = 0
    for name, _typ in returned_columns:
        col = session.query(FunctionColumn).filter_by(
            function_id=function_id, name=name,
        ).first()
        if col is None:
            continue
        for b in session.query(ConceptBinding).filter_by(column_id=col.id).all():
            if b.provenance != "sample-confirmed":
                b.provenance = "sample-confirmed"
                b.reviewed = True
                b.confidence = max(b.confidence, DEFAULT_THRESHOLD)
                count += 1
    session.commit()
    return count


def confirm_by_rule(
    session: Session, dry_run: bool = True, threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """Confirm machine-proposed bindings that carry positive evidence (design D7).

    Evidence is both of: the binding's function declares an entity domain that
    matches the concept's entity type, AND the function's endpoint resolves in
    THIS environment. Confirmation records :data:`CONFIRMED_BY_RULE`, which is
    never a human judgement or a real-fetch sample.

    The rule refuses rather than assumes. A cross-domain binding is the defect
    this change exists to stop. A function that declares no domain supplies no
    positive evidence, so it is left for review. An endpoint this environment
    cannot resolve is left alone too — including when the environment simply
    lacks the crawl libraries, which is reported so a run on a machine without
    them is not mistaken for a verdict on the bindings.

    ``dry_run`` (the default) reports the outcome without writing anything.
    """
    from fd_open_data_mcp.fetch.capability import RESOLVABLE, UNVERIFIABLE, check_command

    counts = {
        "confirmed": 0, "cross_domain": 0, "no_declared_domain": 0,
        "unresolvable": 0, "unverifiable": 0, "below_threshold": 0,
    }
    confirmed_ids: list[int] = []
    unverifiable_sources: set[str] = set()

    rows = (
        session.query(ConceptBinding)
        .filter(ConceptBinding.provenance == "llm",
                ConceptBinding.reviewed.is_(False))
        .all()
    )
    entity_types = _concept_entity_types(session, [b.concept_id for b in rows])

    for binding in rows:
        if binding.confidence < threshold:
            counts["below_threshold"] += 1
            continue

        column = session.get(FunctionColumn, binding.column_id)
        fn = column.function if column is not None else None
        entity_type = entity_types.get(binding.concept_id)
        if entity_type is None or fn is None:
            counts["no_declared_domain"] += 1
            continue

        source = fn.source.name if fn.source is not None else None
        declared = function_domain(fn.command, source)
        if declared is None:
            counts["no_declared_domain"] += 1
            continue
        if declared != entity_type:
            counts["cross_domain"] += 1
            continue

        status, _reason = check_command(source, fn.command)
        if status != RESOLVABLE:
            if status == UNVERIFIABLE:
                counts["unverifiable"] += 1
                if source:
                    unverifiable_sources.add(source)
            else:
                counts["unresolvable"] += 1
            continue

        confirmed_ids.append(binding.id)

    counts["confirmed"] = len(confirmed_ids)
    counts["dry_run"] = dry_run
    counts["unverifiable_sources"] = sorted(unverifiable_sources)

    if dry_run or not confirmed_ids:
        return counts

    # Chunked so a large confirmation set cannot exceed a driver's parameter
    # limit (Postgres caps a statement at 65535 bound parameters).
    for start in range(0, len(confirmed_ids), 500):
        session.query(ConceptBinding).filter(
            ConceptBinding.id.in_(confirmed_ids[start:start + 500])
        ).update(
            {"reviewed": True, "provenance": CONFIRMED_BY_RULE},
            synchronize_session=False,
        )
    session.commit()
    return counts
