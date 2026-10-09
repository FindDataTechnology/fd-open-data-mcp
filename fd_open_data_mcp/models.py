"""SQLAlchemy models for the fd-open-data-mcp ontology store.

Layered schema:

  Catalog (imported from fd-* registries + upstream scan):
    sources, functions, columns (model class FunctionColumn)

  Semantic layer:
    concept_families    (the small curated vocabulary: fd:GDP, fd:Population,
                         ... materialized from the protocol's concepts.yaml)
    concepts            (the Variables - a family qualified by entity_type,
                         measure, unit, frequency; canonical identity is the
                         five-tuple code + entity_type + measure + unit + frequency)
    concept_bindings    (physical column -> concept; confidence + provenance)
    concept_mappings    (concept -> external vocabulary term; SKOS relation)

  Entity identity:
    entity_source_identifiers  (entity -> per-source identifier)

  Ranking:
    source_rankings     (per source x concept: quality, accessibility, freshness-fit)

  Runtime:
    semantic_observations  (read-through concept-keyed cache)
    fetch_log, schedules, executions
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

# Postgres stores JSONB; SQLite (unit tests) can't compile that type, so the
# shared alias degrades to generic JSON there. One instance reused across
# columns is fine — Column copies the type on attach.
JSONB = _PG_JSONB().with_variant(JSON(), "sqlite")

# Postgres platform tables key on BIGSERIAL/bigint; SQLite only autoincrements
# a column declared INTEGER PRIMARY KEY, so the shared alias degrades there
# (same with_variant trick as JSONB above).
Bigint = BigInteger().with_variant(Integer(), "sqlite")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    label = Column(String(128), nullable=False)
    description = Column(String, nullable=True)
    url = Column(String(512), nullable=True)
    scanner_version = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    functions = relationship("Function", back_populates="source", cascade="all, delete-orphan")

    def toDict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "description": self.description,
            "url": self.url, "scanner_version": self.scanner_version,
        }


class Function(Base):
    __tablename__ = "functions"
    __table_args__ = (UniqueConstraint("source_id", "command", name="uq_source_function"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    command = Column(String(255), nullable=False, index=True)
    category = Column(String(255), nullable=True)
    description = Column(String, nullable=True)
    parameters = Column(JSON, nullable=True)
    verified = Column(Boolean, nullable=False, default=True)
    scanner_mode = Column(String(32), nullable=False, default="upstream-curated")
    frequency = Column(String(32), nullable=True)  # daily/weekly/monthly/yearly/irregular/unknown
    real_sources = Column(JSONB, nullable=True)  # real data sources this function calls
    bulk_history = Column(Boolean, nullable=False, default=False)  # one call returns the full dated series (series-mode crawl)
    # fix-silent-zero-yield-crawls: one call returns the full entity cross-section
    # for a single date (snapshot planning — one request instead of one per entity)
    bulk_snapshot = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    source = relationship("Source", back_populates="functions")
    columns = relationship("FunctionColumn", back_populates="function", cascade="all, delete-orphan", lazy="selectin")

    def get_primary_real_source(self) -> Optional[dict]:
        """Get the primary real source (priority=0) or None if not declared."""
        if not self.real_sources:
            return None
        # Sort by priority and return the first one
        sorted_sources = sorted(self.real_sources, key=lambda x: x.get("priority", 0))
        return sorted_sources[0] if sorted_sources else None

    def toDict(self) -> dict:
        return {
            "command": self.command, "category": self.category, "description": self.description,
            "parameters": self.parameters or [], "verified": self.verified,
            "scanner_mode": self.scanner_mode, "frequency": self.frequency,
            "real_sources": self.real_sources, "bulk_history": self.bulk_history,
            "bulk_snapshot": self.bulk_snapshot,
            "columns": [c.toDict() for c in self.columns] if self.columns else [],
        }


class FunctionColumn(Base):
    """A physical output column of a function. Table name is `columns`.

    Class is named FunctionColumn (not Column) to avoid shadowing
    sqlalchemy.Column, which is used throughout this module.
    """

    __tablename__ = "columns"
    __table_args__ = (UniqueConstraint("function_id", "name", name="uq_function_column"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    function_id = Column(Integer, ForeignKey("functions.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    type = Column(String(64), nullable=True)
    description = Column(String, nullable=True)
    meaning = Column(String, nullable=False, default="unknown")  # unknown when desc is "-" / empty
    semantic_type = Column(String(64), nullable=True)  # hint from source (e.g. cn-gov: title/date/url/category)
    frequency = Column(String(32), nullable=True)  # column-level cadence; defaults to the function's
    datasource = Column(String(64), nullable=True)  # column-level source; defaults to the function's (composite cols)
    created_at = Column(DateTime, default=_now)

    function = relationship("Function", back_populates="columns")

    def toDict(self) -> dict:
        return {
            "name": self.name, "type": self.type, "description": self.description,
            "meaning": self.meaning, "semantic_type": self.semantic_type,
            "frequency": self.frequency, "datasource": self.datasource,
        }


class ConceptFamily(Base):
    """A concept family — the first level of the two-level concept model.

    Materialized from the protocol vocabulary's ``concepts.yaml``. Variables in
    ``concepts`` reference a family by ``concept_code``; the reference is loose
    (no hard FK) because derived families may lag the vocabulary.
    """
    __tablename__ = "concept_families"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(128), unique=True, nullable=False, index=True)
    name_en = Column(String(255), nullable=True)
    name_zh = Column(String(255), nullable=True)
    description = Column(String, nullable=True)
    value_type = Column(String(32), nullable=True)   # currency/count/ratio/index/text/date/duration
    unit_type = Column(String(32), nullable=True)    # currency/count/percent/index/none
    dimensions = Column(JSONB, nullable=True)        # qualifier ids that apply to this family
    uri = Column(String(512), nullable=True)         # https://schema.finddata.tech/concept/<code>
    deprecated = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "code": self.code, "name_en": self.name_en,
            "name_zh": self.name_zh, "description": self.description,
            "value_type": self.value_type, "unit_type": self.unit_type,
            "dimensions": self.dimensions, "uri": self.uri,
            "deprecated": self.deprecated,
        }


class Concept(Base):
    __tablename__ = "concepts"
    __table_args__ = (
        UniqueConstraint("code", "entity_type", "measure", "unit", "frequency", name="uq_concept_identity"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(128), nullable=False, index=True)
    name_en = Column(String(255), nullable=True)
    name_zh = Column(String(255), nullable=True)
    category = Column(String(255), nullable=True)
    unit = Column(String(64), nullable=True)
    measure = Column(String(64), nullable=True, default="")  # statistical method/basis: nominal_current/real_constant/ppp/per_capita/growth
    frequency = Column(String(32), nullable=False, default="unknown")
    entity_type = Column(String(32), nullable=False)  # country/city/stock/fund/bond/index/future/crypto/organization/industry
    concept_code = Column(String(128), nullable=True, index=True)  # -> concept_families.code
    source = Column(String(64), nullable=True)  # origin indicator_def source
    verified = Column(Boolean, nullable=False, default=True)
    deprecated = Column(Boolean, nullable=False, default=False)  # retired duplicate; excluded from discovery + dispatch
    created_at = Column(DateTime, default=_now)

    bindings = relationship("ConceptBinding", back_populates="concept", cascade="all, delete-orphan")
    mappings = relationship("ConceptMapping", back_populates="concept", cascade="all, delete-orphan")

    def toDict(self) -> dict:
        return {
            "id": self.id, "code": self.code, "name_en": self.name_en, "name_zh": self.name_zh,
            "category": self.category, "unit": self.unit, "measure": self.measure,
            "frequency": self.frequency, "concept_code": self.concept_code,
            "entity_type": self.entity_type, "source": self.source, "verified": self.verified,
            "deprecated": self.deprecated,
        }


class ConceptBinding(Base):
    __tablename__ = "concept_bindings"
    __table_args__ = (UniqueConstraint("concept_id", "column_id", name="uq_concept_column"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    column_id = Column(Integer, ForeignKey("columns.id", ondelete="CASCADE"), nullable=False, index=True)
    confidence = Column(Float, nullable=False, default=0.0)
    provenance = Column(String(32), nullable=False, default="llm")  # llm/manual/sample-confirmed
    reviewed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now)

    concept = relationship("Concept", back_populates="bindings")
    column = relationship("FunctionColumn")

    def toDict(self) -> dict:
        return {
            "id": self.id, "concept_id": self.concept_id, "column_id": self.column_id,
            "confidence": self.confidence, "provenance": self.provenance, "reviewed": self.reviewed,
        }


class ConceptMapping(Base):
    """A Variable <-> external-vocabulary-term assertion (the crosswalk).

    Same propose-and-confirm governance as ``ConceptBinding``: confidence,
    provenance, and a review flag. ``relation`` is restricted to the SKOS
    mapping properties (see ``fd_open_data_protocol.vocabulary.RELATION_TYPES``).
    """
    __tablename__ = "concept_mappings"
    __table_args__ = (
        UniqueConstraint("concept_id", "vocabulary", "term", "relation", name="uq_concept_mapping"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    vocabulary = Column(String(64), nullable=False, index=True)  # datacommons/worldbank/wikidata/xbrl-us-gaap/sdmx
    term = Column(String(255), nullable=False, index=True)       # the external identifier or code
    relation = Column(String(16), nullable=False)                # exact/close/broader/narrower/related
    confidence = Column(Float, nullable=False, default=0.0)
    provenance = Column(String(32), nullable=False, default="manual")  # registry/manual/llm
    reviewed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=_now)

    concept = relationship("Concept", back_populates="mappings")

    def toDict(self) -> dict:
        return {
            "id": self.id, "concept_id": self.concept_id, "vocabulary": self.vocabulary,
            "term": self.term, "relation": self.relation, "confidence": self.confidence,
            "provenance": self.provenance, "reviewed": self.reviewed,
        }


class EntitySourceIdentifier(Base):
    __tablename__ = "entity_source_identifiers"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "source", name="uq_entity_source"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(32), nullable=False)  # country/city/stock/industry
    entity_id = Column(Integer, nullable=False)       # logical FK into fd-entities-indicators tables
    source = Column(String(64), nullable=False)       # akshare/yfinance/worldbank/...
    identifier = Column(String(255), nullable=False)  # the per-source symbol/code
    created_at = Column(DateTime, default=_now)

    def toDict(self) -> dict:
        return {
            "entity_type": self.entity_type, "entity_id": self.entity_id,
            "source": self.source, "identifier": self.identifier,
        }


# --- Entity Graph (Phase 2: add-entity-graph-vector-search) ------------------------

class Entity(Base):
    """Unified entity registry for all entity types.

    Stores companies, stocks, countries, cities, industries as a single table
    with entity_type discriminator. The graph edges go via entity_relationships.
    """
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(32), nullable=False, index=True)
    code = Column(String(128), nullable=False)
    name_en = Column(String(255), nullable=True)
    name_zh = Column(String(255), nullable=True)
    metadata_json = Column(JSONB, nullable=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("entity_type", "code", name="uq_entity_type_code"),
    )

    def toDict(self) -> dict:
        return {
            "id": self.id, "entity_type": self.entity_type, "code": self.code,
            "name_en": self.name_en, "name_zh": self.name_zh,
            "metadata": self.metadata_json, "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EntityRelationship(Base):
    """Entity-to-entity relationships with temporal validity.

    Supports any relation type (listed_as, operates_in, located_in, member_of, etc.).
    Temporal columns allow tracking changes over time.
    """
    __tablename__ = "entity_relationships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type = Column(String(64), nullable=False, index=True)
    target_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    valid_from = Column(DateTime, nullable=True)
    valid_to = Column(DateTime, nullable=True)
    metadata_json = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("source_id", "relation_type", "target_id", "valid_from", name="uq_rel_edges"),
    )

    def toDict(self) -> dict:
        return {
            "id": self.id, "source_id": self.source_id, "relation_type": self.relation_type,
            "target_id": self.target_id,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "metadata": self.metadata_json,
        }


class SourceRanking(Base):
    __tablename__ = "source_rankings"
    __table_args__ = (UniqueConstraint("source", "concept_id", name="uq_source_concept_rank"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    quality = Column(Float, nullable=False, default=0.5)
    accessibility = Column(Float, nullable=False, default=0.5)
    freshness_fit = Column(Float, nullable=False, default=0.5)  # request-dependent; neutral default
    fetch_count = Column(Integer, nullable=False, default=0)
    fail_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "concept_id": self.concept_id,
            "quality": self.quality, "accessibility": self.accessibility,
            "freshness_fit": self.freshness_fit,
            "fetch_count": self.fetch_count, "fail_count": self.fail_count,
        }


class SemanticObservation(Base):
    __tablename__ = "semantic_observations"
    __table_args__ = (
        # granularity (day|month|year) in the key: a monthly observation of a period
        # and a daily observation of a day inside it are DISTINCT rows, so
        # ON CONFLICT DO NOTHING never silently drops one cadence for another
        # (previously granularity was encoded in the day value: 12-31 / 01 / every day).
        # source_used in the key (add-multi-source-observations): two sources' values
        # for the same point coexist — never merged or overwritten across sources;
        # reads pick the preferred source at query time via source_rankings.
        UniqueConstraint("concept_id", "entity_type", "entity_id", "date", "granularity",
                         "source_used", name="uq_sem_obs"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(Integer, nullable=False)
    date = Column(String(64), nullable=False)  # canonical YYYY-MM-DD (year->12-31, month->01)
    # server_default so raw-SQL writers (tests, migrate) get 'day' too — mirrors the
    # migration's `NOT NULL DEFAULT 'day'`. day | month | year.
    granularity = Column(String(8), nullable=False, default="day", server_default="day")
    value = Column(String(255), nullable=True)
    unit = Column(String(64), nullable=True)
    source_used = Column(String(64), nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "concept_id": self.concept_id, "entity_type": self.entity_type,
            "entity_id": self.entity_id, "date": self.date,
            "granularity": self.granularity, "value": self.value,
            "unit": self.unit, "source_used": self.source_used,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


class FetchLog(Base):
    __tablename__ = "fetch_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    concept_id = Column(Integer, nullable=True, index=True)
    entity_type = Column(String(32), nullable=True)
    entity_id = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False)  # ok / 429 / timeout / 5xx / error
    detail = Column(String, nullable=True)
    # source-proxy-health: which proxy was used and how the outcome was classified.
    proxy_id = Column(Integer, nullable=True, index=True)
    classification = Column(String(16), nullable=True)  # ok / transient / ban / blocked
    real_source = Column(String(64), nullable=True, index=True)  # real data source (e.g., "eastmoney")
    # fix-silent-zero-yield-crawls: the endpoint actually called + the egress it
    # was called from. source/real_source alone cannot locate a failure — one
    # source spans reachable and unreachable hosts simultaneously (D5); the
    # cluster_id is what per-(cluster, function) demotion keys on (pods carry
    # SCRAW_CLUSTER_ID; read()-path rows leave it NULL).
    function_id = Column(Integer, ForeignKey("functions.id"), nullable=True, index=True)
    cluster_id = Column(Integer, nullable=True, index=True)
    timestamp = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "concept_id": self.concept_id,
            "entity_type": self.entity_type, "entity_id": self.entity_id,
            "latency_ms": self.latency_ms, "status": self.status,
            "real_source": self.real_source,
            "detail": self.detail,
            "proxy_id": self.proxy_id, "classification": self.classification,
            "function_id": self.function_id, "cluster_id": self.cluster_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    cron_expr = Column(String(128), nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC")
    enabled = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "concept_id": self.concept_id, "cron_expr": self.cron_expr,
            "timezone": self.timezone, "enabled": self.enabled,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
        }


class Execution(Base):
    __tablename__ = "executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schedule_id = Column(Integer, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=True, index=True)
    concept_id = Column(Integer, nullable=True, index=True)
    status = Column(String(32), nullable=False)  # success / failed / partial
    started_at = Column(DateTime, nullable=False, default=_now)
    finished_at = Column(DateTime, nullable=True)
    detail = Column(String, nullable=True)

    def toDict(self) -> dict:
        return {
            "id": self.id, "schedule_id": self.schedule_id, "concept_id": self.concept_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail,
        }


# --- crawl-control-center (add-fund-crawl-control-center) ---------------------------
# A crawl policy is a *scope* statement: which concepts x which entities x which
# date policy x which frequency get crawled, on what cron, in which executor mode.
# The reconciler (refresh/reconciler.py) executes due policies; the legacy
# `schedules` table above stays dormant (concept-only, no executor binding).

class CrawlPolicy(Base):
    __tablename__ = "crawl_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    concept_ids = Column(JSONB, nullable=False)            # explicit concept id list
    entity_type = Column(String(32), nullable=False)
    entity_ids = Column(JSONB, nullable=True)              # NULL = all entities of the type
    date_policy = Column(JSONB, nullable=False)            # {mode: since_last|trailing|explicit, days?, start?, end?}
    frequency = Column(String(32), nullable=False, default="daily")   # plan hint: daily/weekly/monthly/quarterly
    mode = Column(String(16), nullable=False, default="per_date")     # series | per_date
    source_filter = Column(JSONB, nullable=True)           # NULL = all ranked sources
    force = Column(Boolean, nullable=False, default=False)  # override POLICY_MAX_FETCHES guardrail
    executor = Column(String(16), nullable=False, default="scrapy")  # scrapy | direct
    script = Column(String(255), nullable=True)   # direct: module name to run (e.g. bulk_ingest_financials_aggregate)
    script_args = Column(JSONB, nullable=True)    # direct: CLI args for the script
    cron_expr = Column(String(128), nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC")
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)

    runs = relationship("PolicyRun", back_populates="policy", cascade="all, delete-orphan")

    def toDict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "concept_ids": self.concept_ids, "entity_type": self.entity_type,
            "entity_ids": self.entity_ids, "date_policy": self.date_policy,
            "frequency": self.frequency, "mode": self.mode,
            "source_filter": self.source_filter, "force": self.force,
            "executor": self.executor, "script": self.script,
            "script_args": self.script_args,
            "cron_expr": self.cron_expr, "timezone": self.timezone,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PolicyRun(Base):
    __tablename__ = "policy_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # panel-ops-console: nullable for ad-hoc (origin='adhoc') runs launched
    # without a persistent policy row; policy runs keep a non-null policy_id.
    policy_id = Column(Integer, ForeignKey("crawl_policies.id", ondelete="CASCADE"), nullable=True, index=True)
    origin = Column(String(16), nullable=False, default="policy")  # policy / adhoc
    status = Column(String(32), nullable=False, default="running")  # running / success / failed / refused / cancelled
    plan_json = Column(JSONB, nullable=True)               # the compiled CrawlPlan
    job_ref = Column(String(255), nullable=True)           # "{cluster_name}/{job}" or scrapyd jobid
    cluster_id = Column(Integer, ForeignKey("clusters.id", ondelete="SET NULL"),
                         nullable=True, index=True)         # worker cluster that ran this (None=legacy/scrapyd)
    started_at = Column(DateTime, nullable=False, default=_now)
    finished_at = Column(DateTime, nullable=True)
    detail = Column(String, nullable=True)
    # fix-silent-zero-yield-crawls: run yield. plan_cells is recorded at launch
    # (the planner's emitted cell count); rows_attempted/rows_new are updated
    # INCREMENTALLY by the pod on each pipeline flush keyed by SCRAW_JOB_REF —
    # nullable so "absent" means "the pod never reported" (D1/D2/D3).
    plan_cells = Column(Integer, nullable=True)
    rows_attempted = Column(Integer, nullable=True)
    rows_new = Column(Integer, nullable=True)
    cancelled_by = Column(String(128), nullable=True)  # actor identity when status='cancelled'

    policy = relationship("CrawlPolicy", back_populates="runs")
    cluster = relationship("Cluster")

    def toDict(self) -> dict:
        return {
            "id": self.id, "policy_id": self.policy_id, "origin": self.origin,
            "status": self.status,
            "plan_json": self.plan_json, "job_ref": self.job_ref,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail,
            "cluster_id": self.cluster_id,
            "plan_cells": self.plan_cells,
            "rows_attempted": self.rows_attempted,
            "rows_new": self.rows_new,
            "cancelled_by": self.cancelled_by,
        }


class DataCensus(Base):
    """Latest per-store observation census (add-shard-aware-coverage).

    One row per observation store: the local master table plus each shard
    foreign server. Written ONLY by an explicit census refresh (panel action
    or CLI); pages read these rows and never collect. Shard figures come
    from catalog/stats probes over dblink (approximate_row_count + chunk
    catalog) — never fact-table scans (runbook OOM landmine).
    """
    __tablename__ = "data_census"

    id = Column(Integer, primary_key=True, autoincrement=True)
    store = Column(String(64), unique=True, nullable=False, index=True)
    kind = Column(String(16), nullable=False)          # local | shard
    approx_rows = Column(Integer, nullable=True)       # exact for local, ≈ for shards
    exact = Column(Boolean, nullable=False, default=False)
    total_size_bytes = Column(Integer, nullable=True)
    chunks = Column(Integer, nullable=True)            # Timescale chunks (shards)
    time_range_end = Column(String(64), nullable=True) # chunk-catalog max(range_end)
    error = Column(String, nullable=True)              # probe failure (e.g. no dblink)
    sampled_at = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "store": self.store, "kind": self.kind,
            "approx_rows": self.approx_rows, "exact": self.exact,
            "total_size_bytes": self.total_size_bytes, "chunks": self.chunks,
            "time_range_end": self.time_range_end, "error": self.error,
            "sampled_at": self.sampled_at.isoformat() if self.sampled_at else None,
        }


class ConceptCoverage(Base):
    """Per-concept coverage cache (panel-data-coverage-cache).

    One row per concept with observations; ``/panel/data`` and the
    ``data_stats`` unfiltered detail read these rows instead of aggregating
    6M+ ``semantic_observations`` live (~102 s as previously formulated).
    ``rows`` counts covered observation POINTS (five-column tuple, coexisting
    sources deduped to one), matching ``coverage_by_concept``. Refreshed
    hourly by the panel background task (single-flight across instances via
    advisory lock) and by an explicit panel action; pages compute live only
    while this cache is empty (first-boot bounded fallback).
    """
    __tablename__ = "concept_coverage"

    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"),
                        primary_key=True)
    rows = Column(Integer, nullable=False)           # covered observation points
    latest_date = Column(String(64), nullable=True)  # canonical YYYY-MM-DD
    last_fetch = Column(DateTime, nullable=True)
    sources = Column(Integer, nullable=False, default=0)
    computed_at = Column(DateTime, nullable=False, default=_now)


class CoverageWave(Base):
    """Crawl-coverage expansion wave bookkeeping (expand-crawl-coverage).

    One row per planned backfill wave: a (entity_type, frequency,
    coverage_state) slice of the gap set, materialized as one or more
    DISABLED backfill ``crawl_policies`` (``policy_ids``) launched through
    ``launch_policy`` (the policy_trigger_now path). Wave policies are
    created with ``enabled=False`` so the reconciler's cron path never fires
    them — expansion launches are always one-shot and explicit.

    Status flow: planned -> running -> verifying -> done | paused. ``paused``
    blocks all further wave launches until an operator resumes (spec
    crawl-coverage-expansion: wave gating on verified yield).
    """
    __tablename__ = "coverage_waves"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(32), nullable=False, index=True)
    frequency_bucket = Column(String(32), nullable=False)   # concept.frequency value
    coverage_state = Column(String(16), nullable=False)     # never | stale
    concept_ids = Column(JSONB, nullable=False)             # wave scope (may shrink on re-plan)
    date_policy = Column(JSONB, nullable=False)
    mode = Column(String(16), nullable=False, default="per_date")
    status = Column(String(16), nullable=False, default="planned", index=True)
    policy_ids = Column(JSONB, nullable=True)               # crawl_policies created for this wave
    rows_new = Column(Integer, nullable=True)               # aggregated at verification
    concepts_before = Column(Integer, nullable=True)        # covered count at wave creation
    concepts_after = Column(Integer, nullable=True)         # covered count at closure
    detail = Column(String, nullable=True)                 # pause reason / gate evidence
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "entity_type": self.entity_type,
            "frequency_bucket": self.frequency_bucket,
            "coverage_state": self.coverage_state,
            "concept_ids": self.concept_ids, "date_policy": self.date_policy,
            "mode": self.mode, "status": self.status,
            "policy_ids": self.policy_ids, "rows_new": self.rows_new,
            "concepts_before": self.concepts_before, "concepts_after": self.concepts_after,
            "detail": self.detail,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# --- multi-cluster registry (add-multi-cluster-master-db) --------------------
# The master control plane dispatches crawl plans to a dynamic fleet of worker
# clusters (cloud servers). Adding a server = insert a row + mount its kubeconfig
# as a Secret on the reconciler pod; no code change. ClusterScheduler picks a
# cluster per plan (tags cover the plan's real_sources, egress not circuit-open,
# least open runs, under capacity).

class Cluster(Base):
    """A worker cluster in the multi-cluster fleet. The reconciler launches crawl
    Jobs here via its k8s API (api_server + token/ca from the kubeconfig Secret
    named by ``kubeconfig_secret``, mounted at /kubeconfigs/<name>/). Workers are
    stateless: read a plan ConfigMap, fetch, upsert into the shared master PG."""
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    api_server = Column(String(255), nullable=False)        # https://<host>:6443
    namespace = Column(String(64), nullable=False, default="scraw")
    image = Column(String(255), nullable=False,
                   default="harbor.local/lawcraw_business/scraw-fd-open-data-mcp:latest")
    tags = Column(JSONB, nullable=True)                     # real_sources this cluster can fetch, e.g. ["eastmoney","sina"]
    capacity = Column(Integer, nullable=False, default=4)   # max concurrent open runs
    kubeconfig_secret = Column(String(128), nullable=True)  # k8s Secret name holding this cluster's kubeconfig
    enabled = Column(Boolean, nullable=False, default=True)
    # Runtime hints for Jobs launched on this cluster (china-cheap onboarding):
    # {"database_url": ..., "redis_url": ..., "dns_nameservers": [...]} — pods on
    # nodes whose ONLY reachable path to the canonical DB is a relay (e.g. the
    # aliyun NodePort forwarders) or whose pod-overlay DNS is unreachable.
    runtime_hints = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "api_server": self.api_server,
            "namespace": self.namespace, "image": self.image, "tags": self.tags,
            "capacity": self.capacity, "kubeconfig_secret": self.kubeconfig_secret,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --- source-proxy-health (add-source-proxy-health) -------------------------------------
# Reliability binds to (source, proxy_id), not source alone: a ban is an IP-level
# event, so the same source may be fine through proxy B while proxy A is blocked.

class Proxy(Base):
    """An upstream proxy IP in the pool. `scheme='direct'` means no upstream proxy
    (the cluster's own egress), ranked first so proxies are only used when direct is
    banned. Permanently-banned proxies are retired; replacements start clean."""
    __tablename__ = "proxies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scheme = Column(String(16), nullable=False)  # direct / http / https / socks5
    ip = Column(String(64), nullable=False)      # "direct" for scheme=direct, else the IP
    port = Column(Integer, nullable=True)
    auth = Column(String(255), nullable=True)    # "user:pass" or None
    status = Column(String(16), nullable=False, default="active")  # active / retired
    label = Column(String(64), nullable=True)
    cluster_id = Column(Integer, ForeignKey("clusters.id", ondelete="SET NULL"),
                         nullable=True, index=True)         # per-cluster direct egress (None = shared/legacy)
    # panel-ops-console: mirror the provider columns fd-proxy-service migration
    # 001 added to the same physical table (the crawler never writes them; the
    # panel proxy page groups rows by owner). Types must match that migration:
    # it declares `provider TEXT`, so a bounded String here would make a freshly
    # built database diverge from the live one.
    provider = Column(Text, nullable=True)         # owning provider (gost-own, paid-static, …)
    provider_meta = Column(JSONB, nullable=True)
    # fd-proxy-service migration 002_mihomo_routing_concurrency.sql: per-address
    # global in-flight cap; NULL = the provider default (itself NULL = uncapped).
    max_concurrency = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    retired_at = Column(DateTime, nullable=True)

    def toDict(self) -> dict:
        return {
            "id": self.id, "scheme": self.scheme, "ip": self.ip, "port": self.port,
            "status": self.status, "label": self.label, "cluster_id": self.cluster_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "retired_at": self.retired_at.isoformat() if self.retired_at else None,
        }


class SourceProxyHealth(Base):
    """Cold aggregate of per-(source, proxy_id) circuit health. Hot state (state,
    fail_streak, cooldown_until) is mirrored in Redis by the circuit updater; this
    table is the auditable record + the source of `accessibility` derivation."""
    __tablename__ = "source_proxy_health"
    __table_args__ = (
        UniqueConstraint("source", "proxy_id", name="uq_source_proxy"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    proxy_id = Column(Integer, ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False, index=True)
    state = Column(String(16), nullable=False, default="closed")  # closed / open / half_open
    fail_streak = Column(Integer, nullable=False, default=0)
    success_streak = Column(Integer, nullable=False, default=0)
    last_fetch_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    banned_at = Column(DateTime, nullable=True)
    cooldown_until = Column(DateTime, nullable=True)
    open_cycles = Column(Integer, nullable=False, default=0)  # K-counter for permanent retirement
    permanent = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "proxy_id": self.proxy_id, "state": self.state,
            "fail_streak": self.fail_streak, "success_streak": self.success_streak,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "banned_at": self.banned_at.isoformat() if self.banned_at else None,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "open_cycles": self.open_cycles, "permanent": self.permanent,
        }


class BanRule(Base):
    """Per-source ban-classification rule. Matched in priority order (desc); first
    match wins. rule_type: status (http status code/pattern), error (exception
    message substring), body (response body regex). classification: ok / transient
    / ban / blocked / permanent. `streak_min` gates the rule (e.g.
    RemoteDisconnected -> ban only after streak >= 3). `permanent` marks a failure
    intrinsic to the request (the endpoint does not exist), not a route-health
    signal - it must not move a proxy circuit."""
    __tablename__ = "ban_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    rule_type = Column(String(16), nullable=False)  # status / error / body
    pattern = Column(String(255), nullable=False)
    classification = Column(String(16), nullable=False)  # ok / transient / ban / blocked / permanent
    streak_min = Column(Integer, nullable=False, default=0)
    priority = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "rule_type": self.rule_type,
            "pattern": self.pattern, "classification": self.classification,
            "streak_min": self.streak_min, "priority": self.priority, "enabled": self.enabled,
        }


class SourceRateLimit(Base):
    """Per-source politeness rate limit (token bucket). Distinct from refresh
    frequency scheduling (scheduled-refresh). Enforced at fetch time against
    Redis `rate:{source}:{proxy_id}`."""
    __tablename__ = "source_rate_limits"
    __table_args__ = (
        UniqueConstraint("source", name="uq_source_rate_limit"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    max_qps = Column(Float, nullable=False, default=1.0)
    max_concurrent = Column(Integer, nullable=False, default=4)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "max_qps": self.max_qps,
            "max_concurrent": self.max_concurrent,
        }


class SourceProbe(Base):
    """Per-source probe command used by the probe job to test whether a banned
    ``(source, proxy_id)`` has recovered. Data-driven (not code) so onboarding a
    new source is a row insert, not a code change. ``params`` is a JSON dict of
    the upstream call's kwargs (a cheap, known-good fetch - the result is not
    used, only whether the call is classified as a ban)."""
    __tablename__ = "source_probes"
    __table_args__ = (
        UniqueConstraint("source", name="uq_source_probe"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    command = Column(String(128), nullable=False)
    params = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "command": self.command,
            "params": self.params, "enabled": self.enabled,
        }


# --- Retrofitted from the live database -----------------------------------------
# These two tables exist in the canonical database and are read/written with raw
# SQL, but no model described them — they were created by ad-hoc scripts under
# scripts/. Declared here so the schema can be built from code alone. Column
# definitions, unique constraints and index names match the live tables so a
# freshly built database can be compared against the canonical one.

class EntityEmbedding(Base):
    """Vector embedding of an entity, keyed ``(entity_id, model)``.

    Created by ``scripts/generate_entity_embeddings.py``; read by
    ``embeddings/generator.py`` and ``semantic/entity_search.py``. ``embedding``
    holds a serialised vector as TEXT (not JSONB — the two embedding tables
    differ on this).
    """
    __tablename__ = "entity_embeddings"
    __table_args__ = (
        UniqueConstraint("entity_id", "model", name="entity_embeddings_entity_id_model_key"),
        Index("idx_entity_embeddings_entity_id", "entity_id"),
        Index("idx_entity_embeddings_model", "model"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(Integer, nullable=False)
    embedding = Column(Text, nullable=False)
    model = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "entity_id": self.entity_id,
            "model": self.model, "created_at": self.created_at,
        }


class ConceptEmbedding(Base):
    """Vector embedding of a concept, keyed ``(concept_id, model)``.

    Created by ``scripts/migrate_add_vector_search.py``; read by ``ai_search.py``
    and ``semantic_search.py``. ``embedding`` is JSONB (``entity_embeddings``
    stores TEXT instead).
    """
    __tablename__ = "concept_embeddings"
    __table_args__ = (
        UniqueConstraint("concept_id", "model", name="concept_embeddings_concept_id_model_key"),
        Index("idx_concept_embeddings_concept_id", "concept_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_id = Column(Integer, nullable=False)
    embedding = Column(JSONB, nullable=False)
    model = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "concept_id": self.concept_id,
            "model": self.model, "created_at": self.created_at,
        }


# --- crawl platform control tables (crawl-platform) ---------------------------
# Central control-plane tables shared with the spider platform dispatcher
# (same database/schema as crawl_policies). The dispatcher mirrors every
# spiders/*/manifest.yaml into crawl_sources; operators/agents trigger work by
# inserting pending_runs and ask for teardown via crawl_runs.cancel_requested.
# Column names match the deployed DDL (change crawl-platform task 2.3) exactly;
# the tables already exist in the canonical PG, the models exist so tests
# (Base.metadata.create_all on sqlite) and the panel/MCP read+write paths share
# one schema definition. Timestamps are TIMESTAMPTZ in PG; readers treat naive
# values as UTC (the same writer contract snapshot._as_aware_utc documents).

PLATFORM_PENDING_STATUSES = ("pending", "claimed", "done", "failed", "cancelled")
# crawl_runs.status values (runners write these; cancel_requested is a flag)
PLATFORM_RUN_STATUSES = ("running", "success", "failed", "cancelled", "skipped")


class CrawlSite(Base):
    """A member site of the crawl platform federation (tencent, nbs-workers,
    law, ...). Sources reference a site; triggers fail loudly when the site is
    not registered here (FK)."""
    __tablename__ = "crawl_sites"

    # Column shapes mirror the deployed central DDL exactly (adopted into the
    # chain by 0006_control_plane_adoption; reports/adoption-diff.md is the
    # per-column reconciliation record).
    id = Column(Text, primary_key=True)               # e.g. "tencent"
    description = Column(Text, nullable=False, server_default="")
    kind = Column(Text, nullable=False, server_default="docker")
    enabled = Column(Boolean, nullable=False, server_default=true())
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())

    def toDict(self) -> dict:
        return {
            "id": self.id, "description": self.description, "kind": self.kind,
            "enabled": self.enabled,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class CrawlSource(Base):
    """One spider of the platform, mirrored from the content repo's
    spiders/*/manifest.yaml by the dispatcher (``kind``='platform'), or a
    federated member registered by seed (``kind``='federated': the law line's
    CronJobs — scheduling stays in its own chart, the control plane only
    reads the runner declaration and queues pending_runs). ``schedule`` NULL
    means the source is registered but not lit up (未点亮) — no automatic
    runs; federated members are always platform-unlit by definition."""
    __tablename__ = "crawl_sources"

    source = Column(Text, primary_key=True)
    # No index on site: the deployed table carries none (adoption-diff #5) —
    # crawl_sources is a few dozen rows, a seq scan beats an index.
    site = Column(Text, ForeignKey("crawl_sites.id"), nullable=True)
    schedule = Column(Text, nullable=True)            # NULL = 未点亮
    # Per-source timezone the ``schedule`` cron is matched in (0009). NULL =
    # UTC, the historical default the whole fleet is calibrated to; a source
    # whose window only works at a local hour (login-gated crawls needing a
    # human at the keyboard) sets e.g. 'Asia/Shanghai'. An unknown/invalid tz
    # degrades to UTC (never raises).
    schedule_tz = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=true())
    last_commit = Column(Text, nullable=True)
    # session-pool: the source's login profile (spiders/<source>/login.py
    # declares it); NULL = anonymous crawl, no identity pool consulted.
    auth_profile = Column(Text, nullable=True)
    # legal-line-federation: execution-body registration. 'platform' rows are
    # dispatcher-mirrored manifest sources; 'federated' rows carry a runner
    # declaration (image + command + timeout) the dispatcher executes verbatim
    # when a pending_runs row is claimed. frozen_reason set = 注册冻结: the
    # trigger refuses with the reason (a retired member stays visible).
    kind = Column(Text, nullable=False, default="platform",
                  server_default="platform")          # 'platform' | 'federated'
    runner_image = Column(Text, nullable=True)        # full image ref incl. tag
    runner_command = Column(JSONB, nullable=True)     # argv list, e.g. ["node","bin/x.mjs"]
    timeout_seconds = Column(Integer, nullable=True)  # declared job deadline
    # legal-line-federation (0008): Secret names the dispatcher envFrom's into
    # this source's Job — mechanical injection (RustFS credentials etc.), not
    # source business logic. NULL = nothing injected.
    runner_env_from = Column(JSONB, nullable=True)
    frozen_reason = Column(Text, nullable=True)       # set = frozen (不可触发)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())

    @property
    def runner_declared(self) -> bool:
        """A complete runner declaration (both image and argv) is present —
        the minimum for the dispatcher to execute a federated row."""
        return bool(self.runner_image and self.runner_command)

    def toDict(self) -> dict:
        return {
            "source": self.source, "site": self.site, "schedule": self.schedule,
            "schedule_tz": self.schedule_tz,
            "enabled": self.enabled, "last_commit": self.last_commit,
            "auth_profile": self.auth_profile,
            "kind": self.kind, "runner_image": self.runner_image,
            "runner_command": self.runner_command,
            "timeout_seconds": self.timeout_seconds,
            "runner_env_from": self.runner_env_from,
            "frozen_reason": self.frozen_reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CrawlRun(Base):
    """A platform run reported to crawl_runs (all sites). ``cancel_requested``
    is the cooperative-cancel checkpoint flag: control planes only SET it; the
    runner observes it and closes its own row as cancelled."""
    __tablename__ = "crawl_runs"

    id = Column(Bigint, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False)
    kind = Column(Text, nullable=False, default="runtime", server_default="runtime")
    status = Column(Text, nullable=False)  # running|success|failed|cancelled|skipped
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    # Prod column is NOT NULL DEFAULT 0 (unknown yield records 0, the same
    # convention federation rows use); the model must match or sqlite fixtures
    # silently allow NULLs that production rejects (2026-09-27 incident).
    rows_written = Column(Integer, nullable=False, server_default="0")
    error_head = Column(Text, nullable=True)
    commit_sha = Column(Text, nullable=True)
    image_tag = Column(Text, nullable=True)
    cancel_requested = Column(DateTime(timezone=True), nullable=True)
    pending_run_id = Column(Bigint, nullable=True)    # trigger lineage, no hard FK
    # session-pool: the crawl_identities.account_alias whose leased session the
    # run was injected with (NULL = anonymous run). Alias, not FK — the pool
    # table is keyed (source, account_alias) and rows outlive runs.
    identity_alias = Column(Text, nullable=True)
    # legal-line-federation (telemetry contract): quality metrics the runner
    # self-reports at close (loader: effective_body_ratio / coverage_count).
    # NULL = not reported (mirror-only rows, legacy runners).
    metrics = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())

    # Deployed index (adoption-diff #6): source-scoped run history, newest
    # first — DESC participates in alembic's index comparison, so it is
    # declared, not just documented. No standalone status/source indexes:
    # production has none. Sits after the columns: it references created_at.
    __table_args__ = (
        Index("crawl_runs_source_idx", "source", created_at.desc()),
    )

    def toDict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "rows_written": self.rows_written, "error_head": self.error_head,
            "commit_sha": self.commit_sha, "image_tag": self.image_tag,
            "cancel_requested": (self.cancel_requested.isoformat()
                                 if self.cancel_requested else None),
            "pending_run_id": self.pending_run_id,
            "identity_alias": self.identity_alias,
            "metrics": self.metrics,
        }


class CrawlItem(Base):
    """Per-run payload rows (crawl_items): item batch jsonb keyed (run, idx)."""
    __tablename__ = "crawl_items"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "idx", name="crawl_items_pkey"),
    )

    run_id = Column(Bigint, ForeignKey("crawl_runs.id", ondelete="CASCADE"),
                    nullable=False)
    idx = Column(Integer, nullable=False)
    payload = Column(JSONB, nullable=False)

    def toDict(self) -> dict:
        return {"run_id": self.run_id, "idx": self.idx, "payload": self.payload}


class PendingRun(Base):
    """A requested-but-not-yet-executed platform run. Control planes (panel /
    MCP tools) INSERT; the site dispatcher atomically claims (status pending ->
    claimed + lease), executes, and writes back the terminal status."""
    __tablename__ = "pending_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','claimed','done','failed','cancelled')",
            name="pending_runs_status_check"),
        Index("pending_runs_site_idx", "site", "status", "created_at"),
    )

    id = Column(Bigint, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False)
    site = Column(Text, ForeignKey("crawl_sites.id"), nullable=False,
                  server_default="tencent")
    params = Column(JSONB, nullable=False, default=dict)  # param overrides; prod DEFAULT '{}' (sqlite can't compile the ::jsonb server default)
    requested_by = Column(Text, nullable=False, default="console",
                          server_default="console")
    status = Column(Text, nullable=False, default="pending",
                    server_default="pending")
    claimed_by = Column(Text, nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=2, server_default="2")
    run_id = Column(Bigint, nullable=True)            # -> crawl_runs.id once started
    error_head = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def toDict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "site": self.site,
            "params": self.params, "requested_by": self.requested_by,
            "status": self.status, "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "lease_until": self.lease_until.isoformat() if self.lease_until else None,
            "attempts": self.attempts, "max_attempts": self.max_attempts,
            "run_id": self.run_id, "error_head": self.error_head,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# --- authenticated-crawling identity pool (session-pool) -----------------------
# Mirror of the central identity-pool tables (created by change session-pool
# task 1.1 in the central fd_open_data database). Column names match the
# deployed DDL exactly; the panel reads these read-only (logins happen on the
# login site) while the auth_request_login MCP tool only registers rows in
# login_required + note events. Timestamps are TIMESTAMPTZ in PG; sqlite tests
# read them back naive (the writer contract snapshot._as_aware_utc documents).
IDENTITY_STATUSES = ("login_required", "active", "cooldown", "banned", "retired")
IDENTITY_EVENT_KINDS = (
    "login", "probe", "small_batch", "lease_acquired", "lease_released",
    "lease_expired", "auth_failed", "suspect_yield", "banned", "note",
)


class CrawlIdentity(Base):
    """One account identity in a source's pool (crawl_identities).

    Five-state machine: login_required -> active -> (cooldown | banned |
    retired); UNIQUE(source, account_alias). The lease_* columns implement the
    table-semantics atomic lease (claim writes owner + token + TTL; TTL expiry
    is lazily re-claimable). Sessions live in object storage (session_ref);
    credentials in k8s secrets (credentials_secret_ref) — neither here."""
    __tablename__ = "crawl_identities"
    __table_args__ = (
        # Constraint/index names are the deployed ones (adoption-diff #9) so
        # the adopted revision, production and these models stay byte-equal.
        UniqueConstraint("source", "account_alias",
                         name="crawl_identities_source_account_alias_key"),
        CheckConstraint(
            "status in ('login_required','active','cooldown','banned','retired')",
            name="crawl_identities_status_check"),
        CheckConstraint("automation in ('auto','assisted')",
                        name="crawl_identities_automation_check"),
        Index("crawl_identities_pool_idx", "source", "status"),
        Index("crawl_identities_lease_idx", "lease_expires_at",
              postgresql_where=text("lease_token IS NOT NULL")),
    )

    id = Column(Bigint, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False)
    account_alias = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="login_required",
                    server_default="login_required")
    automation = Column(Text, nullable=False, default="assisted",
                        server_default="assisted")  # auto | assisted
    egress_ref = Column(Text, nullable=True)            # identity-level egress binding
    credentials_secret_ref = Column(Text, nullable=True)
    session_ref = Column(Text, nullable=True)           # encrypted jar in RustFS
    lease_owner = Column(Text, nullable=True)
    lease_token = Column(Text, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_probe_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    consecutive_zero_runs = Column(Integer, nullable=False, default=0,
                                   server_default="0")
    failure_count = Column(Integer, nullable=False, default=0,
                           server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, onupdate=_now, server_default=func.now())

    def toDict(self) -> dict:
        return {
            "id": self.id, "source": self.source,
            "account_alias": self.account_alias, "status": self.status,
            "automation": self.automation, "egress_ref": self.egress_ref,
            "lease_owner": self.lease_owner,
            "lease_expires_at": (self.lease_expires_at.isoformat()
                                 if self.lease_expires_at else None),
            "last_login_at": (self.last_login_at.isoformat()
                              if self.last_login_at else None),
            "last_success_at": (self.last_success_at.isoformat()
                                if self.last_success_at else None),
            "consecutive_zero_runs": self.consecutive_zero_runs,
            "failure_count": self.failure_count,
        }


class CrawlIdentityEvent(Base):
    """Audit event for one identity (crawl_identity_events) — the replayable
    login/probe/lease trail the spec requires. lease_token correlates the
    event with the lease it belongs to."""
    __tablename__ = "crawl_identity_events"

    id = Column(Bigint, primary_key=True, autoincrement=True)
    identity_id = Column(Bigint,
                         ForeignKey("crawl_identities.id", ondelete="CASCADE"),
                         nullable=False)
    kind = Column(Text, nullable=False)
    detail = Column(Text, nullable=True)
    lease_token = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())

    # Deployed index (adoption-diff #10); DESC is compared by alembic, hence
    # declared on the column object (placed after the columns it references).
    __table_args__ = (
        CheckConstraint(
            "kind in ('login','probe','small_batch','lease_acquired',"
            "'lease_released','lease_expired','auth_failed','suspect_yield',"
            "'banned','note')",
            name="crawl_identity_events_kind_check"),
        Index("crawl_identity_events_identity_idx", "identity_id",
              created_at.desc()),
    )

    def toDict(self) -> dict:
        return {
            "id": self.id, "identity_id": self.identity_id, "kind": self.kind,
            "detail": self.detail, "lease_token": self.lease_token,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --- login stations (login-station-console) -------------------------------------
# One row per login-station launch. Written by the panel/orchestration side
# (station_ops.create_station: launching -> waiting_operator) and by the
# station runtime itself (login_station.py reports completed/failed). Column
# names match the central DDL exactly (station_ops.STATION_DDL is the shared
# source of truth; the fd-industry-data runtime creates it centrally).
STATION_STATUSES = (
    "launching", "waiting_operator", "completed", "failed", "timeout",
    "reclaimed",
)


class CrawlLoginStation(Base):
    """A login station in flight (crawl_login_stations).

    Status machine: launching -> waiting_operator -> completed | failed; the
    orchestration side may close an overdue station as timeout (deadline
    backstop) or reclaimed (operator reclaim / teardown). The identity keeps
    its own five-state machine — a station never touches it beyond events."""
    __tablename__ = "crawl_login_stations"
    __table_args__ = (
        CheckConstraint(
            "status in ('launching','waiting_operator','completed','failed',"
            "'timeout','reclaimed')",
            name="crawl_login_stations_status_check"),
    )

    id = Column(Bigint, primary_key=True, autoincrement=True)
    identity_id = Column(Bigint,
                         ForeignKey("crawl_identities.id", ondelete="CASCADE"),
                         nullable=False)
    source = Column(Text, nullable=False)
    account_alias = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="launching",
                    server_default="launching")
    proxy_url = Column(Text, nullable=True)     # the identity egress the station dials through
    note = Column(Text, nullable=True)          # job/service/progress notes (runtime overwrites)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=_now, server_default=func.now())
    deadline_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    def toDict(self) -> dict:
        return {
            "id": self.id, "identity_id": self.identity_id,
            "source": self.source, "account_alias": self.account_alias,
            "status": self.status, "proxy_url": self.proxy_url,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# --- source-discovery pipeline mirror tables (harness-platform-integration) ---
# Read-only mirrors of the discovery pipeline tables that live in the central
# fd_open_data database (created and written by the fd-scraw-harness side:
# discover → candidates → analyses → manifests). Column names match the
# deployed central DDL exactly; unlike the crawl-platform tables above, the
# panel/MCP never write these — approval happens on the harness tool surface.
# Timestamps are TIMESTAMP WITHOUT TIME ZONE (naive-UTC writer contract), so
# the models use plain DateTime. The approved-manifest -> crawl_sources landed
# linkage is a QUERY-TIME fact (source_name == crawl_sources.source): no FK,
# no schema change (spec source-discovery-pipeline).
MANIFEST_STATUSES = ("draft", "approved", "rejected")


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Discovery(Base):
    """One source-discovery run (a query for new data sources)."""
    __tablename__ = "discoveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    query = Column(Text, nullable=False)
    query_type = Column(String(16), nullable=False)   # e.g. topic|domain
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=_now_naive)
    updated_at = Column(DateTime, default=_now_naive)

    def toDict(self) -> dict:
        return {
            "id": self.id, "query": self.query, "query_type": self.query_type,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Candidate(Base):
    """A candidate data source surfaced by one discovery run."""
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    discovery_id = Column(Integer, ForeignKey("discoveries.id"), nullable=False)
    url = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    estimated_data_type = Column(String(64), nullable=True)
    score = Column(Integer, nullable=True)
    coverage_status = Column(String(32), nullable=True)
    coverage_details = Column(Text, nullable=True)
    adjusted_score = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_now_naive)

    def toDict(self) -> dict:
        return {
            "id": self.id, "discovery_id": self.discovery_id, "url": self.url,
            "title": self.title, "description": self.description,
            "estimated_data_type": self.estimated_data_type,
            "score": self.score, "coverage_status": self.coverage_status,
            "coverage_details": self.coverage_details,
            "adjusted_score": self.adjusted_score,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Analysis(Base):
    """Deep page analysis of one candidate (DOM snapshot, endpoints, tables)."""
    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    discovery_id = Column(Integer, ForeignKey("discoveries.id"), nullable=False)
    page_url = Column(Text, nullable=False)
    page_title = Column(Text, nullable=True)
    dom_snapshot = Column(Text, nullable=True)
    api_endpoints = Column(Text, nullable=True)
    data_tables = Column(Text, nullable=True)
    download_links = Column(Text, nullable=True)
    forms = Column(Text, nullable=True)
    raw_analysis = Column(Text, nullable=True)
    status = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=_now_naive)

    def toDict(self) -> dict:
        return {
            "id": self.id, "candidate_id": self.candidate_id,
            "discovery_id": self.discovery_id, "page_url": self.page_url,
            "page_title": self.page_title, "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SourceManifest(Base):
    """A generated crawler manifest awaiting or carrying approval. Its
    source_name links (query-time only) to crawl_sources once landed."""
    __tablename__ = "manifests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    discovery_id = Column(Integer, ForeignKey("discoveries.id"), nullable=False)
    analysis_id = Column(Integer, ForeignKey("analyses.id"), nullable=True)
    manifest_yaml = Column(Text, nullable=False)
    source_name = Column(String(128), nullable=True)
    model_used = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False)  # draft|approved|rejected
    validation_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now_naive)
    updated_at = Column(DateTime, default=_now_naive)

    def toDict(self) -> dict:
        return {
            "id": self.id, "discovery_id": self.discovery_id,
            "analysis_id": self.analysis_id, "manifest_yaml": self.manifest_yaml,
            "source_name": self.source_name, "model_used": self.model_used,
            "status": self.status, "validation_error": self.validation_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Scope(Base):
    """A named retrieval scope (indicator-scope spec): allow-lists over the
    registry/source dimensions. ``rules`` is a single JSON object —
    ``{source_dbs: [], domains: [], semantic_codes: [], native_codes: []}`` —
    CRUD stays atomic and per-item statistics are not needed (design D3)."""
    __tablename__ = "scopes"
    # Uniqueness rides the deployed UNIQUE CONSTRAINT (0004's inline UNIQUE),
    # not a unique index — see reports/adoption-diff.md §3 (scopes 口径裁决).
    __table_args__ = (
        UniqueConstraint("name", name="scopes_name_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    rules = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "rules": self.rules,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScopeBinding(Base):
    """A caller's default scope (design D5): applies when the caller passes no
    explicit scope; ``caller_key`` is an opaque string (token name / panel
    user / integration id) — the deployment decides the granularity."""
    __tablename__ = "scope_bindings"

    caller_key = Column(String(255), primary_key=True)
    scope_name = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def toDict(self) -> dict:
        return {
            "caller_key": self.caller_key, "scope_name": self.scope_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ScopeStat(Base):
    """Per-scope daily hit counters (design D6): one row per (scope, day),
    incremented per scoped retrieval — quantifies the cost saved."""
    __tablename__ = "scope_stats"
    __table_args__ = (
        PrimaryKeyConstraint("scope_name", "day", name="pk_scope_stats_scope_day"),
    )

    scope_name = Column(String(128), nullable=False)
    day = Column(String(10), nullable=False)  # 'YYYY-MM-DD' — portable across PG/SQLite
    calls = Column(Integer, nullable=False, default=0)
    results_returned = Column(Integer, nullable=False, default=0)

    def toDict(self) -> dict:
        return {
            "scope_name": self.scope_name, "day": self.day,
            "calls": self.calls, "results_returned": self.results_returned,
        }
