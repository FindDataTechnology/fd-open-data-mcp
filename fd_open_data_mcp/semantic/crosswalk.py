"""Concept crosswalk: Variable <-> external vocabulary term mappings.

Each mapping asserts that a FindData Variable corresponds to a term in an
external vocabulary (Data Commons DCID, World Bank indicator code, SDMX, XBRL
tag, Wikidata property) with a SKOS-style relation.

Governance mirrors ``concept_bindings`` — confidence + provenance + reviewed:

  - ``registry`` provenance comes from the shipped ``crosswalks/*.yaml``
  - ``manual``   from an operator via ``record_concept_mapping``
  - ``llm``      from a model

Mappings below the review threshold that nobody has reviewed are flagged
``pending_review`` in listings. Mappings inform; they do not drive fetching.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from sqlalchemy.orm import Session

from fd_open_data_protocol.vocabulary import RELATION_TYPES, load_vocabulary

from fd_open_data_mcp.models import Concept, ConceptMapping
from fd_open_data_mcp.semantic.mapper_llm import DEFAULT_THRESHOLD


class InvalidRelation(ValueError):
    """Raised when a mapping declares a relation outside the SKOS mapping set."""


def validate_relation(relation: str) -> str:
    """Return ``relation`` if it is a SKOS mapping property, else raise."""
    if relation not in RELATION_TYPES:
        raise InvalidRelation(
            f"invalid relation '{relation}'; expected one of {', '.join(RELATION_TYPES)}"
        )
    return relation


def record_mapping(
    session: Session,
    concept_id: int,
    vocabulary: str,
    term: str,
    relation: str,
    confidence: float = 1.0,
    provenance: str = "manual",
    reviewed: bool = False,
) -> tuple[ConceptMapping, bool]:
    """Upsert a mapping. Returns ``(row, created)``.

    Uniqueness is the four-field key (variable, vocabulary, term, relation), so
    re-recording the same assertion updates in place instead of duplicating.
    """
    validate_relation(relation)
    row = session.query(ConceptMapping).filter_by(
        concept_id=concept_id, vocabulary=vocabulary, term=term, relation=relation,
    ).first()
    created = row is None
    if row is None:
        row = ConceptMapping(
            concept_id=concept_id, vocabulary=vocabulary, term=term, relation=relation,
        )
        session.add(row)
    row.confidence = confidence
    row.provenance = provenance
    row.reviewed = reviewed
    session.flush()
    return row, created


def import_crosswalks(
    session: Session, vocab_dir: Optional[Union[str, Path]] = None,
) -> dict:
    """Ingest the shipped crosswalk assertions with ``registry`` provenance.

    Assertions naming a Variable that does not exist in the store are reported
    as unresolved rather than created — run ``consume-concepts`` first.
    """
    vocab = load_vocabulary(vocab_dir)
    created = 0
    unresolved: list[str] = []

    for m in vocab.mappings:
        concept = session.query(Concept).filter_by(
            code=m["code"], entity_type=m["entity_type"],
            measure=m.get("measure") or "", unit=m.get("unit") or "",
            frequency=m.get("frequency") or "unknown",
        ).first()
        if concept is None:
            unresolved.append(f"{m['code']}/{m['entity_type']}")
            continue
        _, is_new = record_mapping(
            session, concept.id, m["vocabulary"], m["term"], m["relation"],
            confidence=float(m.get("confidence", 1.0)), provenance="registry",
        )
        if is_new:
            created += 1

    session.commit()
    return {
        "vocabulary_version": vocab.version,
        "created": created,
        "total": len(vocab.mappings),
        "unresolved": unresolved,
    }


def mapping_to_dict(
    row: ConceptMapping, threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """A mapping plus its Variable context and pending-review flag."""
    d = row.toDict()
    d["pending_review"] = (not row.reviewed) and row.confidence < threshold
    c = row.concept
    if c is not None:
        d["variable"] = {
            "code": c.code, "name_en": c.name_en, "entity_type": c.entity_type,
            "measure": c.measure, "unit": c.unit, "frequency": c.frequency,
            "family": c.concept_code,
        }
    return d


def list_mappings(
    session: Session,
    concept_id: Optional[int] = None,
    vocabulary: Optional[str] = None,
    term: Optional[str] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict]:
    """List mappings forward (by Variable) or in reverse (by external term)."""
    q = session.query(ConceptMapping)
    if concept_id is not None:
        q = q.filter(ConceptMapping.concept_id == concept_id)
    if vocabulary:
        q = q.filter(ConceptMapping.vocabulary == vocabulary)
    if term:
        q = q.filter(ConceptMapping.term == term)
    return [mapping_to_dict(m, threshold) for m in q.all()]
