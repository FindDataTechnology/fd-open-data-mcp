"""Column->concept binding management: propose, threshold-gate, confirm, review.

Propose-and-confirm policy (design.md D4):
  - Bindings carry ``confidence`` + ``provenance`` (llm / manual / sample-confirmed).
  - Bindings below the confidence threshold are retained in a review queue and
    excluded from dispatch.
  - A real fetch that returns a matching column promotes a binding to
    ``sample-confirmed`` (design.md D4, spec semantic-layer).
  - Two columns are never silently merged; each keeps its own binding.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, ConceptBinding, FunctionColumn
from fd_open_data_mcp.semantic.mapper_llm import DEFAULT_THRESHOLD, propose_concept


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
    below = 0
    for col in session.query(FunctionColumn).all():
        prop = propose_concept(col.name, col.description, col.semantic_type)
        if not prop:
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
    return {"proposed": proposed, "below_threshold": below}


def dispatch_candidates(
    session: Session, concept_id: int, threshold: float = DEFAULT_THRESHOLD,
) -> list[ConceptBinding]:
    """Bindings eligible for dispatch: sample-confirmed/manual, or confidence >= threshold."""
    rows = session.query(ConceptBinding).filter_by(concept_id=concept_id).all()
    return [
        b for b in rows
        if b.provenance in ("manual", "sample-confirmed") or b.confidence >= threshold
    ]


def review_queue(session: Session, threshold: float = DEFAULT_THRESHOLD) -> list[ConceptBinding]:
    """Below-threshold, unreviewed, llm-proposed bindings awaiting confirmation."""
    rows = session.query(ConceptBinding).filter_by(reviewed=False, provenance="llm").all()
    return [b for b in rows if b.confidence < threshold]


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
