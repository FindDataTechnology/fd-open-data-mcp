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
    timestamp = Column(DateTime, nullable=False, default=_now)

    def toDict(self) -> dict:
        return {
            "source": self.source, "concept_id": self.concept_id,
            "entity_type": self.entity_type, "entity_id": self.entity_id,
            "latency_ms": self.latency_ms, "status": self.status,
            "detail": self.detail,
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
