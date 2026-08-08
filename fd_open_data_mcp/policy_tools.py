"""Crawl-policy MCP tools (add-fund-crawl-control-center, task 6.1).

Exposes policy CRUD + trigger-now + runs list as FastMCP tools. Trigger-now
bypasses the cron schedule but still goes through the single-flight and
plan-size guardrails (spec crawl-control-center).
"""
from __future__ import annotations

import datetime as dt
import json
import os

from croniter import croniter
from fastmcp import FastMCP
from sqlalchemy.orm import Session

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh.reconciler import (
    POLICY_MAX_FETCHES, _default_launcher, build_date_range, estimate_fetches,
    launch_policy,
)


def register_policy_tools(mcp: FastMCP) -> None:
    """Attach the policy tools to the given FastMCP instance."""

    def _session() -> Session:
        return get_database().get_session()

    def _validate(payload: dict) -> None:
        """Raise ValueError on invalid cron_expr / date_policy / concept_ids."""
        if not payload.get("concept_ids"):
            raise ValueError("concept_ids must be a non-empty list")
        try:
            croniter(payload["cron_expr"])
        except (KeyError, ValueError) as e:
            raise ValueError(f"invalid cron_expr: {payload.get('cron_expr')}") from e
        dp = payload.get("date_policy") or {}
        if dp.get("mode") not in ("since_last", "trailing", "explicit", None):
            raise ValueError(f"invalid date_policy.mode: {dp.get('mode')}")

    @mcp.tool
    def policy_create(
        name: str,
        concept_ids: list[int],
        entity_type: str,
        cron_expr: str = "0 6 * * *",
        timezone: str = "UTC",
        entity_ids: list[int] | None = None,
        date_policy: dict | None = None,
        frequency: str = "daily",
        mode: str = "per_date",
        source_filter: list[str] | None = None,
        force: bool = False,
        enabled: bool = True,
    ) -> dict:
        """Create a crawl policy.

        Args:
            name: Unique policy name.
            concept_ids: List of concept IDs to crawl.
            entity_type: Entity type (fund/stock/country/...).
            cron_expr: 5-field cron expression (default daily at 06:00 UTC).
            timezone: Timezone for cron evaluation (default UTC).
            entity_ids: Optional list of entity IDs (NULL = all entities of the type).
            date_policy: Date range policy: {mode: since_last|trailing|explicit, days?, start?, end?}.
            frequency: Plan frequency hint: daily/weekly/monthly/quarterly/yearly (default daily).
            mode: Crawl mode: series (one fetch per concept×entity) or per_date (default).
            source_filter: Optional list of source names to restrict to (NULL = all ranked sources).
            force: Override POLICY_MAX_FETCHES guardrail (default False).
            enabled: Enable the policy (default True).

        Returns:
            The created policy as a dict.
        """
        payload = {
            "name": name, "concept_ids": concept_ids, "entity_type": entity_type,
            "cron_expr": cron_expr, "timezone": timezone, "entity_ids": entity_ids,
            "date_policy": date_policy or {"mode": "since_last"}, "frequency": frequency,
            "mode": mode, "source_filter": source_filter, "force": force, "enabled": enabled,
        }
        _validate(payload)
        s = _session()
        try:
            p = CrawlPolicy(**payload)
            s.add(p)
            s.commit()
            s.refresh(p)
            return p.toDict()
        finally:
            s.close()

    @mcp.tool
    def policy_list(enabled: bool | None = None) -> list[dict]:
        """List crawl policies. Optional filter by enabled status."""
        s = _session()
        try:
            q = s.query(CrawlPolicy)
            if enabled is not None:
                q = q.filter_by(enabled=enabled)
            return [p.toDict() for p in q.order_by(CrawlPolicy.id).all()]
        finally:
            s.close()

    @mcp.tool
    def policy_get(policy_id: int) -> dict:
        """Get a single crawl policy by ID."""
        s = _session()
        try:
            p = s.query(CrawlPolicy).get(policy_id)
            if not p:
                raise ValueError(f"policy {policy_id} not found")
            return p.toDict()
        finally:
            s.close()

    @mcp.tool
    def policy_update(
        policy_id: int,
        name: str | None = None,
        concept_ids: list[int] | None = None,
        entity_type: str | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
        entity_ids: list[int] | None = None,
        date_policy: dict | None = None,
        frequency: str | None = None,
        mode: str | None = None,
        source_filter: list[str] | None = None,
        force: bool | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """Update a crawl policy. Only provided fields are changed."""
        s = _session()
        try:
            p = s.query(CrawlPolicy).get(policy_id)
            if not p:
                raise ValueError(f"policy {policy_id} not found")
            updates = {
                "name": name, "concept_ids": concept_ids, "entity_type": entity_type,
                "cron_expr": cron_expr, "timezone": timezone, "entity_ids": entity_ids,
                "date_policy": date_policy, "frequency": frequency, "mode": mode,
                "source_filter": source_filter, "force": force, "enabled": enabled,
            }
            for k, v in updates.items():
                if v is not None:
                    setattr(p, k, v)
            _validate({
                "name": p.name, "concept_ids": p.concept_ids, "entity_type": p.entity_type,
                "cron_expr": p.cron_expr, "timezone": p.timezone, "entity_ids": p.entity_ids,
                "date_policy": p.date_policy, "frequency": p.frequency, "mode": p.mode,
                "source_filter": p.source_filter, "force": p.force, "enabled": p.enabled,
            })
            s.commit()
            s.refresh(p)
            return p.toDict()
        finally:
            s.close()

    @mcp.tool
    def policy_enable(policy_id: int) -> dict:
        """Enable a crawl policy."""
        return policy_update(policy_id, enabled=True)

    @mcp.tool
    def policy_disable(policy_id: int) -> dict:
        """Disable a crawl policy."""
        return policy_update(policy_id, enabled=False)

    @mcp.tool
    def policy_delete(policy_id: int) -> dict:
        """Delete a crawl policy and all its runs (cascade)."""
        s = _session()
        try:
            p = s.query(CrawlPolicy).get(policy_id)
            if not p:
                raise ValueError(f"policy {policy_id} not found")
            s.delete(p)
            s.commit()
            return {"deleted": policy_id, "name": p.name}
        finally:
            s.close()

    @mcp.tool
    def policy_trigger_now(policy_id: int) -> dict:
        """Trigger a crawl for the policy immediately.

        Bypasses the cron schedule but still enforces single-flight (skips if
        an open run exists) and the plan-size guardrail (refuses if estimate
        exceeds POLICY_MAX_FETCHES unless force=true).
        """
        s = _session()
        try:
            p = s.query(CrawlPolicy).get(policy_id)
            if not p:
                raise ValueError(f"policy {policy_id} not found")
            launcher = _default_launcher()
            result = launch_policy(s, p, launcher)
            return result
        finally:
            s.close()

    @mcp.tool
    def policy_runs(
        policy_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List policy runs. Optional filters: policy_id, status (running/success/failed)."""
        s = _session()
        try:
            q = s.query(PolicyRun)
            if policy_id is not None:
                q = q.filter_by(policy_id=policy_id)
            if status is not None:
                q = q.filter_by(status=status)
            runs = q.order_by(PolicyRun.started_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id, "policy_id": r.policy_id, "status": r.status,
                    "job_ref": r.job_ref, "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "detail": r.detail, "plan_json": r.plan_json,
                }
                for r in runs
            ]
        finally:
            s.close()

    @mcp.tool
    def policy_estimate(
        concept_ids: list[int],
        entity_type: str,
        entity_ids: list[int] | None = None,
        date_policy: dict | None = None,
        frequency: str = "daily",
        mode: str = "per_date",
        source_filter: list[str] | None = None,
    ) -> dict:
        """Estimate the fetch count for a policy payload (used by the panel editor).

        Returns {"estimate": int, "unroutable": [...], "unmapped": [...], "policy_max_fetches": int}.
        """
        from fd_open_data_mcp.crawl.plan import EntityScope
        from fd_open_data_mcp.crawl.planner import plan_crawl

        s = _session()
        try:
            dp = date_policy or {"mode": "since_last"}
            # build a transient CrawlPolicy-like object for build_date_range
            class _P:
                pass
            p = _P()
            p.date_policy = dp
            p.frequency = frequency
            dr, since_last = build_date_range(p, dt.date.today())
            plan = plan_crawl(
                s, concept_ids, EntityScope(entity_type=entity_type, entity_ids=entity_ids),
                dr, since_last=since_last, source_filter=source_filter, mode=mode,
            )
            return {
                "estimate": estimate_fetches(s, plan),
                "unroutable": [u.model_dump(mode="json") for u in plan.unroutable],
                "unmapped": [u.model_dump(mode="json") for u in plan.unmapped],
                "policy_max_fetches": POLICY_MAX_FETCHES,
            }
        finally:
            s.close()
