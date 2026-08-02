"""SQLAlchemy models for the fd-open-data-mcp ontology store.

Layered schema:

  Catalog (imported from fd-* registries + upstream scan):
    sources, functions, columns (model class FunctionColumn)

  Semantic layer:
    concepts            (consumed from fd-entities-indicators/indicator_defs;
                         canonical identity = code + entity_type + unit + frequency)
    concept_bindings    (physical column -> concept; confidence + provenance)

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
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


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
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    source = relationship("Source", back_populates="functions")
    columns = relationship("FunctionColumn", back_populates="function", cascade="all, delete-orphan", lazy="selectin")

    def toDict(self) -> dict:
        return {
            "command": self.command, "category": self.category, "description": self.description,
            "parameters": self.parameters or [], "verified": self.verified,
            "scanner_mode": self.scanner_mode, "frequency": self.frequency,
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
    source = Column(String(64), nullable=True)  # origin indicator_def source
    verified = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_now)

    bindings = relationship("ConceptBinding", back_populates="concept", cascade="all, delete-orphan")

    def toDict(self) -> dict:
        return {
            "id": self.id, "code": self.code, "name_en": self.name_en, "name_zh": self.name_zh,
            "category": self.category, "unit": self.unit, "measure": self.measure,
            "frequency": self.frequency,
            "entity_type": self.entity_type, "source": self.source, "verified": self.verified,
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
        UniqueConstraint("concept_id", "entity_type", "entity_id", "date", name="uq_sem_obs"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(Integer, nullable=False)
    date = Column(String(64), nullable=False)
    value = Column(String(255), nullable=True)
    unit = Column(String(64), nullable=True)
    source_used = Column(String(64), nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "concept_id": self.concept_id, "entity_type": self.entity_type,
            "entity_id": self.entity_id, "date": self.date, "value": self.value,
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
    timestamp = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "concept_id": self.concept_id,
            "entity_type": self.entity_type, "entity_id": self.entity_id,
            "latency_ms": self.latency_ms, "status": self.status,
            "detail": self.detail,
            "proxy_id": self.proxy_id, "classification": self.classification,
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
    created_at = Column(DateTime, nullable=False, default=_now)
    retired_at = Column(DateTime, nullable=True)

    def toDict(self) -> dict:
        return {
            "id": self.id, "scheme": self.scheme, "ip": self.ip, "port": self.port,
            "status": self.status, "label": self.label,
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
    / ban / blocked. `streak_min` gates the rule (e.g. RemoteDisconnected -> ban
    only after streak >= 3)."""
    __tablename__ = "ban_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    rule_type = Column(String(16), nullable=False)  # status / error / body
    pattern = Column(String(255), nullable=False)
    classification = Column(String(16), nullable=False)  # ok / transient / ban / blocked
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
