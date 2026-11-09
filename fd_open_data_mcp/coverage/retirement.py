"""Retirement blast radius for concepts (knowledge-surface-integrity R2).

A concept is retired by flipping ``Concept.deprecated``. The read path already
does the right thing once that flag is set: ``check_applicability`` raises
``ConceptDeprecated`` carrying the canonical successor, and the planner records
it as ``canonical_replacement`` in ``unroutable``. What was missing is the step
BEFORE: nothing could answer "who depends on this concept", so retirements
happened blind and a policy silently lost its routable set (the 54-day stall).

This module answers that question and refuses a blind retirement. It never
rewrites a policy: a successor is not necessarily equivalent (unit, frequency
and entity domain can all differ), so auto-rebinding would fabricate an
equivalence that does not exist.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, ConceptBinding, CrawlPolicy

logger = logging.getLogger(__name__)


class RetirementBlocked(Exception):
    """A concept still has live dependents; retiring it would break them blind."""

    def __init__(self, concept: Concept, dependents: dict):
        self.concept = concept
        self.dependents = dependents
        names = ", ".join(p["name"] for p in dependents["policies"]) or "-"
        super().__init__(
            f"concept {concept.code} (id={concept.id}) still has dependents: "
            f"policies=[{names}], bindings={dependents['bindings']}. "
            f"Retire with force=True only after deciding what those policies "
            f"should do — this never rewrites them for you."
        )


def dependents(session: Session, concept_id: int) -> dict:
    """Everything that currently points at this concept. Read-only.

    ``policies`` is the authoritative dependency list: ``crawl_policies.concept_ids``
    is what the planner reads. ``bindings`` is the count of column bindings that
    would keep routing it; both must be considered before retiring.
    """
    policies = []
    for p in session.query(CrawlPolicy).all():
        ids = p.concept_ids or []
        if isinstance(ids, str):  # defensive: some writers store JSON text
            import json
            try:
                ids = json.loads(ids)
            except (TypeError, ValueError):
                ids = []
        if concept_id in ids:
            policies.append({
                "id": p.id, "name": p.name, "enabled": bool(p.enabled),
                "entity_type": p.entity_type,
            })
    bindings = (session.query(ConceptBinding)
                .filter(ConceptBinding.concept_id == concept_id).count())
    return {"policies": policies, "bindings": bindings}


def retire_concept(session: Session, concept_id: int, *,
                   force: bool = False) -> dict:
    """Retire a concept, refusing when live dependents exist unless forced.

    Retirement is a flag flip, never a delete: the row stays resolvable so old
    identifiers keep answering "retired, successor X" instead of "not found".

    Deliberately does NOT touch ``crawl_policies.concept_ids`` — auto-rebinding
    a policy to the successor would silently swap one indicator for another that
    may differ in unit, frequency or entity domain.
    """
    concept = session.get(Concept, concept_id)
    if concept is None:
        raise ValueError(f"concept {concept_id} not found")

    deps = dependents(session, concept_id)
    live = [p for p in deps["policies"] if p["enabled"]]
    if live and not force:
        raise RetirementBlocked(concept, deps)

    from fd_open_data_mcp.entities.resolver import find_canonical_replacement
    successor = find_canonical_replacement(session, concept)
    concept.deprecated = True
    session.flush()
    logger.warning(
        "concept %s retired (force=%s); dependents: policies=%s bindings=%s successor=%s",
        concept.code, force,
        [p["name"] for p in deps["policies"]], deps["bindings"],
        successor.code if successor else None,
    )
    return {
        "concept_id": concept_id,
        "code": concept.code,
        "deprecated": True,
        "successor": {"id": successor.id, "code": successor.code} if successor else None,
        "dependents": deps,
    }
