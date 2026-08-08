"""Pydantic schema for the ``CrawlPlan`` artifact (the planner -> executor contract).

A plan declares the wanted concepts (each with its ranked source/binding chain), the
entity scope, the date range, and the persistence target. It also carries reports for
concepts that could not be routed (no confirmed binding) and entities with no
per-source identifier (graceful degradation, design.md D3/D6).

The plan is deliberately lazy: ``entity_scope.entity_ids`` is ``None`` for a filter
scope (e.g. "all stocks") - the executor expands the universe at crawl time.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PlanSource(BaseModel):
    """One candidate ``(source, function, column)`` for a concept, in rank order."""

    source: str
    score: float
    function_id: int
    function_command: str
    column_name: str
    binding_id: int
    confidence: float


class PlanConcept(BaseModel):
    """A wanted concept with its ranked source chain + failover order."""

    concept_id: int
    code: str
    entity_type: str
    unit: Optional[str] = None
    frequency: Optional[str] = None
    ranked_sources: list[PlanSource]


class EntityScope(BaseModel):
    """Entity universe: a type plus either an explicit id list or a filter (None = all)."""

    entity_type: str
    entity_ids: Optional[list[int]] = None  # None -> all (filter); executor expands
    filter: Optional[str] = None


class DateRange(BaseModel):
    # start is Optional so the CLI/MCP can pass None when --since-last is used;
    # the planner computes the watermark and fills start before the plan is returned
    # (a plan with start=None only survives in the no-prior-data early-return, which
    # carries no wanted_concepts, so the executor never expands it).
    start: Optional[str] = None
    end: str
    frequency: Optional[str] = None  # cadence hint for the executor


class CrawlPlan(BaseModel):
    version: str = "1"
    # Fetch strategy: "per_date" (one request per concept x entity x date) or
    # "series" (one request per concept x entity against a bulk_history endpoint;
    # the pipeline explodes the returned frame, design D6).
    mode: str = "per_date"
    wanted_concepts: list[PlanConcept]
    entity_scope: EntityScope
    date_range: DateRange
    unroutable: list[dict] = Field(default_factory=list)  # concepts refused (no binding / mismatch / mode)
    unmapped: list[dict] = Field(default_factory=list)     # (entity, source) pairs with no identifier
    persistence: dict = Field(default_factory=lambda: {"table": "semantic_observations"})

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
