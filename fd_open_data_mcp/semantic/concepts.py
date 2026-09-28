"""Seed the two-level concept model from the protocol vocabulary.

The vocabulary shipped by ``fd-open-data-protocol`` (``vocabulary/concepts.yaml``)
is the source of truth for both levels:

  - **Concept families** -> ``concept_families``
  - **Variables**        -> ``concepts`` (the five-tuple identity is unchanged:
    ``code + entity_type + measure + unit + frequency``)

The legacy ``fd-entities-indicators/indicator_defs`` sqlite source is gone — its
absence on every current checkout is why the concepts table was empty.

Family assignment follows design.md D4, in precedence order:

  1. explicit family reference (the vocabulary's ``variables:`` block, or a
     manifest concept hint's ``concept_family``)
  2. the concept ``code`` normalized-matching a declared family — longest
     segment prefix wins, so ``price.close`` -> ``PriceClose`` and
     ``population.total`` -> ``Population``
  3. otherwise a family-of-one keyed by the code, labelled from the Variable
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional, Union
from pathlib import Path

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from fd_open_data_protocol.vocabulary import VocabularyRegistry, load_vocabulary

from fd_open_data_mcp.models import Concept, ConceptFamily
from fd_open_data_mcp.semantic.registry_catalog import read_verified_entries


@lru_cache(maxsize=1)
def _shipped_vocabulary() -> VocabularyRegistry:
    """The installed vocabulary — the authority for curated family identity."""
    return load_vocabulary()


def _vocabulary_families() -> dict[str, str]:
    """``_normalize(family id) -> family id`` for the curated families."""
    return {_normalize(e.id): e.id for e in _shipped_vocabulary().concept_families()}


def _normalize(text: str) -> str:
    """Lowercase and drop non-alphanumerics: ``price.close`` -> ``priceclose``."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _camel(segments: list[str]) -> str:
    return "".join(s[:1].upper() + s[1:] for s in segments if s)


def derive_family_code(concept_code: str, families_by_norm: dict[str, str]) -> str:
    """Derive a family id for a Variable code with no explicit family.

    Tries progressively shorter prefixes of the code's segments against known
    families (``families_by_norm`` maps ``_normalize(family_id) -> family_id``);
    falls back to a family-of-one keyed by the whole code.
    """
    segments = [s for s in re.split(r"[^A-Za-z0-9]+", concept_code or "") if s]
    for end in range(len(segments), 0, -1):
        known = families_by_norm.get(_normalize(_camel(segments[:end])))
        if known:
            return known
    return _camel(segments) or concept_code


def ensure_family(
    session: Session,
    code: str,
    *,
    label_en: Optional[str] = None,
    label_zh: Optional[str] = None,
) -> ConceptFamily:
    """Return the family ``code``, creating a family-of-one if it is absent.

    Only non-None fields are written, so re-seeding never clobbers operator
    edits with vocabulary blanks.
    """
    fam = session.query(ConceptFamily).filter_by(code=code).first()
    if fam is None:
        fam = ConceptFamily(code=code)
        session.add(fam)
    if label_en is not None:
        fam.name_en = label_en
    if label_zh is not None:
        fam.name_zh = label_zh
    session.flush()
    return fam


def families_by_norm(session: Session) -> dict[str, str]:
    """``_normalize(family code) -> family code`` for every family in the store."""
    return {_normalize(f.code): f.code for f in session.query(ConceptFamily).all()}


def materialize_family(session: Session, entry) -> ConceptFamily:
    """Upsert a family row from a vocabulary entry.

    Only fields the vocabulary actually declares are written, so a blank
    vocabulary field never clobbers a non-null operator value.
    """
    fam = ensure_family(session, entry.id, label_en=entry.label_en, label_zh=entry.label_zh)
    for attr, value in (
        ("description", entry.description),
        ("value_type", entry.get("value_type")),
        ("unit_type", entry.get("unit_type")),
        ("dimensions", entry.get("dimensions")),
        ("uri", entry.uri),
    ):
        if value is not None:
            setattr(fam, attr, value)
    session.flush()
    return fam


def assign_family(
    session: Session, concept: Concept, explicit: Optional[str] = None,
) -> str:
    """Assign ``concept.concept_code`` using the D4 rule. Returns the family code.

    Candidates come from the curated vocabulary *and* the store, so a Variable
    registered before ``consume-concepts`` runs still links to its declared
    family instead of spawning a family-of-one.
    """
    if explicit:
        ensure_family(session, explicit)
        concept.concept_code = explicit
        return explicit

    known = {**_vocabulary_families(), **families_by_norm(session)}
    code = derive_family_code(concept.code, known)

    if session.query(ConceptFamily).filter_by(code=code).first() is None:
        entry = _shipped_vocabulary().get("concept", code)
        if entry is not None:
            materialize_family(session, entry)
        else:
            ensure_family(session, code, label_en=concept.name_en, label_zh=concept.name_zh)

    concept.concept_code = code
    return code


def consume_indicator_defs(
    session: Session, vocab_dir: Optional[Union[str, Path]] = None,
) -> dict:
    """Seed families + Variables from the protocol vocabulary. Idempotent.

    ``vocab_dir`` overrides the shipped vocabulary directory (tests point it at
    a fixture). Names keep the historical ``consume_indicator_defs`` /
    ``consume-concepts`` spelling so CLI and tool surfaces are unchanged.
    """
    vocab: VocabularyRegistry = load_vocabulary(vocab_dir)

    families_created = 0
    for entry in vocab.concept_families():
        existed = session.query(ConceptFamily).filter_by(code=entry.id).first() is not None
        materialize_family(session, entry)
        if not existed:
            families_created += 1

    variables_created = 0
    for seed in vocab.seeded_variables():
        concept = session.query(Concept).filter_by(
            code=seed.code, entity_type=seed.entity_type, measure=seed.measure,
            unit=seed.unit, frequency=seed.frequency,
        ).first()
        if concept is None:
            concept = Concept(
                code=seed.code, entity_type=seed.entity_type, measure=seed.measure,
                unit=seed.unit, frequency=seed.frequency, verified=True, source="vocabulary",
            )
            session.add(concept)
            session.flush()
            variables_created += 1
        if concept.concept_code != seed.family_id:
            concept.concept_code = seed.family_id

    # Backfill: no Variable is left without a family (spec concept-variable-model).
    assigned = 0
    for concept in session.query(Concept).filter(Concept.concept_code.is_(None)).all():
        assign_family(session, concept)
        assigned += 1

    session.commit()
    return {
        "vocabulary_version": vocab.version,
        "families_created": families_created,
        "families_total": session.query(ConceptFamily).count(),
        "variables_created": variables_created,
        "variables_total": session.query(Concept).count(),
        "families_assigned": assigned,
        "errors": [],
    }


def _family_labels(session: Session) -> dict[str, str]:
    """``family code -> display label`` for every materialized family."""
    return {
        f.code: (f.name_en or f.name_zh or f.code)
        for f in session.query(ConceptFamily).all()
    }


LIST_MAX_LIMIT = 1000
LIST_DEFAULT_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    """Clamp a page size into [1, LIST_MAX_LIMIT]."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return LIST_DEFAULT_LIMIT
    return max(1, min(n, LIST_MAX_LIMIT))


def _registry_concept_row(entry: dict) -> dict:
    """Render a unified-registry entry as a catalog row (plain dict).

    Same key set as ``Concept.toDict()`` + the family label, so callers can
    consume native and registry rows uniformly; identity is additive —
    ``native_code`` / ``source_db`` exist only on registry-derived rows, and
    ``id`` is ``None`` because these are not ``concepts`` table rows (do not
    use it for ``concept_id`` lookups).
    """
    return {
        "id": None,
        "code": entry.get("semantic_code"),
        "name_en": entry.get("name_en"),
        "name_zh": entry.get("name_zh"),
        "category": entry.get("domain"),
        "unit": entry.get("unit"),
        "measure": None,
        "frequency": entry.get("frequency"),
        "concept_code": None,
        "concept_family_label": None,
        "entity_type": None,
        "source": "registry",
        "verified": True,
        "deprecated": False,
        "native_code": entry.get("native_code"),
        "source_db": entry.get("source_db"),
    }


def _entry_matches_query(entry: dict, query: Optional[str]) -> bool:
    """Case-insensitive substring match over an entry's identity/name fields."""
    needle = (query or "").strip().lower()
    if not needle:
        return True
    fields = (
        entry.get("semantic_code"), entry.get("name_en"),
        entry.get("name_zh"), entry.get("native_code"),
    )
    return any(v and needle in str(v).lower() for v in fields)


def list_concepts_with_family(
    session: Session,
    entity_type: Optional[str] = None,
    concept_family: Optional[str] = None,
    limit: int = LIST_DEFAULT_LIMIT,
    offset: int = 0,
    query: Optional[str] = None,
) -> list[dict]:
    """Variables with their family identifier + label, optionally filtered.

    Paginated: ``limit`` is clamped to [1, LIST_MAX_LIMIT] and ``offset`` to
    >= 0, ordered deterministically by (entity_type, code, id) so offset paging
    neither skips nor duplicates rows within a catalog version. A caller pages
    until a page returns fewer than ``limit`` rows — no total is returned, so
    the result shape stays a plain list.

    The catalog is the union of the native ``concepts`` table and the verified
    entries of the unified indicator registry (``registry_entries``, read
    fail-soft — see ``registry_catalog``). Registry rows are appended after the
    native rows, ordered by (domain, semantic_code, native_code); an entry
    whose semantic_code equals a NON-deprecated native concept's code is
    deduplicated (native wins), while deprecated native rows — legacy imports
    the registry supersedes — never shadow a verified entry. Registry rows
    carry additive ``native_code`` / ``source_db`` fields. They have no
    entity_type / concept family, so ``entity_type`` / ``concept_family``
    filters (and only those) exclude them — a query can find an entry by either
    its native code or its semantic_code.

    Args:
        query: optional case-insensitive substring matched against
            code / name_en / name_zh (and native_code for registry rows);
            ``None`` keeps the unfiltered behavior.
    """
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    q = session.query(Concept)
    if entity_type:
        q = q.filter(Concept.entity_type == entity_type)
    if concept_family:
        q = q.filter(Concept.concept_code == concept_family)
    if query and query.strip():
        like = f"%{query.strip()}%"
        q = q.filter(or_(
            Concept.code.ilike(like),
            Concept.name_en.ilike(like),
            Concept.name_zh.ilike(like),
        ))

    labels = _family_labels(session)
    out = []
    rows = (
        q.order_by(Concept.entity_type, Concept.code, Concept.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    for c in rows:
        row = c.toDict()
        row["concept_family_label"] = labels.get(c.concept_code or "")
        out.append(row)

    # Unified-registry tail: read fail-soft; an absent table yields [] and
    # the page below stays exactly the pre-registry native result.
    if entity_type is None and concept_family is None:
        entries = read_verified_entries(session)
        if query and query.strip():
            entries = [e for e in entries if _entry_matches_query(e, query)]
        if entries:
            # Dedupe against NON-deprecated natives only: the concepts table
            # carries ~1.5k deprecated rows from a legacy WDI import, and a
            # verified registry entry with the same code supersedes/revives
            # that code instead of being shadowed by the retired row.
            native_codes = {
                code for (code,) in q.with_entities(Concept.code)
                .filter(Concept.deprecated.is_(False)).distinct()
            }
            entries = [e for e in entries if e.get("semantic_code") not in native_codes]
            total_native = q.order_by(None).count()
            # Merged order is [native rows][registry rows]; slice the
            # registry region of [offset, offset+limit) accordingly.
            reg_start = max(0, offset - total_native)
            reg_end = max(0, offset + limit - total_native)
            out.extend(
                _registry_concept_row(e) for e in entries[reg_start:reg_end]
            )
    return out


def list_concept_families(
    session: Session, limit: int = LIST_DEFAULT_LIMIT, offset: int = 0
) -> list[dict]:
    """Concept families with the number of Variables assigned to each, paginated.

    Same ``limit``/``offset`` semantics as ``list_concepts_with_family``
    (ordered by family code).
    """
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    counts = dict(
        session.query(Concept.concept_code, func.count(Concept.id))
        .group_by(Concept.concept_code)
        .all()
    )
    families = (
        session.query(ConceptFamily)
        .order_by(ConceptFamily.code)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {**f.toDict(), "variable_count": counts.get(f.code, 0)}
        for f in families
    ]
