"""fd-open-data-mcp CLI (Click).

Mirrors the MCP tools for shell use. Run ``fd-open-data-mcp --help``.
"""
from __future__ import annotations

import json

import click

from fd_open_data_mcp import __version__


def _echo(obj) -> None:
    click.echo(json.dumps(obj, ensure_ascii=False, default=str, indent=2))


@click.group()
@click.version_option(__version__)
def cli():
    """fd-open-data-mcp: open-data ontology MCP."""


@cli.command("migrate")
def migrate_cmd():
    """Create all DB tables (idempotent)."""
    from fd_open_data_mcp.migrate import migrate

    r = migrate()
    click.echo(f"Initialized {r['table_count']} tables at {r['database_url']}")


@cli.command("serve")
def serve_cmd():
    """Run the FastMCP server (stdio transport)."""
    from fd_open_data_mcp.server import main

    main()


@cli.command("import-catalog")
@click.argument("provider", required=False)
def import_catalog_cmd(provider):
    """Import one (by name) or all fd-* provider registries."""
    from fd_open_data_mcp.catalog.importer import import_all, import_provider

    if provider:
        _echo(import_provider(provider))
    else:
        for r in import_all():
            _echo(r)


@cli.command("consume-concepts")
def consume_concepts_cmd():
    """Consume indicator_defs into the concepts table."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.semantic.concepts import consume_indicator_defs

    s = get_database().get_session()
    try:
        _echo(consume_indicator_defs(s))
    finally:
        s.close()


@cli.command("propose-bindings")
def propose_bindings_cmd():
    """Propose column->concept bindings."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.semantic.bindings import propose_bindings

    s = get_database().get_session()
    try:
        _echo(propose_bindings(s))
    finally:
        s.close()


@cli.command("seed-entities")
def seed_entities_cmd():
    """Seed akshare/yfinance/worldbank entity identifier mappings."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.entities.resolver import (
        seed_country_identifiers, seed_stock_identifiers,
    )

    s = get_database().get_session()
    try:
        _echo({"stocks": seed_stock_identifiers(s), "countries": seed_country_identifiers(s)})
    finally:
        s.close()


@cli.command("rank-sources")
@click.option("--concept-id", type=int, required=True)
@click.option("--requested-date", default=None)
def rank_sources_cmd(concept_id, requested_date):
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept

    s = get_database().get_session()
    try:
        _echo(rank_sources_for_concept(s, concept_id, requested_date))
    finally:
        s.close()


@cli.command("read")
@click.option("--concept-id", type=int, required=True)
@click.option("--entity-type", required=True)
@click.option("--entity-id", type=int, required=True)
@click.option("--date", "dates", multiple=True, required=True)
def read_cmd(concept_id, entity_type, entity_id, dates):
    """Read a concept for an entity over one or more --date values."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.fetch.dispatch import read as _read

    s = get_database().get_session()
    try:
        _echo(_read(s, concept_id, entity_type, entity_id, list(dates)))
    finally:
        s.close()


@cli.command("generate-schedules")
def generate_schedules_cmd():
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.refresh.scheduler import generate_schedules

    s = get_database().get_session()
    try:
        _echo(generate_schedules(s))
    finally:
        s.close()


@cli.command("plan-crawl")
@click.option("--concept-id", "concept_ids", type=int, multiple=True, required=True,
              help="Wanted concept id (repeatable).")
@click.option("--entity-type", required=True, help="Entity type of the scope.")
@click.option("--entity-id", "entity_ids", type=int, multiple=True,
              help="Explicit entity id (repeatable). Omit for a filter/all scope.")
@click.option("--start", required=True, help="Date range start (e.g. 2024-01-01).")
@click.option("--end", required=True, help="Date range end (e.g. 2024-12-31).")
@click.option("--frequency", default=None, help="Cadence hint for the executor.")
@click.option("--out", "-o", default=None, help="Write the plan to this file (JSON).")
def plan_crawl_cmd(concept_ids, entity_type, entity_ids, start, end, frequency, out):
    """Plan a concept crawl -> CrawlPlan (concepts in, methods out)."""
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl as _plan
    from fd_open_data_mcp.db import get_database

    s = get_database().get_session()
    try:
        plan = _plan(
            s, list(concept_ids),
            EntityScope(entity_type=entity_type, entity_ids=list(entity_ids) or None),
            DateRange(start=start, end=end, frequency=frequency),
        )
    finally:
        s.close()
    data = plan.model_dump(mode="json")
    if out:
        with open(out, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        click.echo(
            f"CrawlPlan: {len(plan.wanted_concepts)} concept(s), "
            f"{len(plan.unroutable)} unroutable, {len(plan.unmapped)} unmapped -> {out}"
        )
    else:
        _echo(data)


@cli.command("migrate-data")
@click.argument("source", type=click.Choice([
    "astock", "astock-hk", "astock-us",
    "astock-balance", "astock-profit", "astock-cashflow",
]))
@click.option("--symbol", default=None, help="Scope to one symbol (sampling/verification).")
@click.option("--limit", type=int, default=None, help="Cap rows per column (sampling).")
def migrate_data_cmd(source, symbol, limit):
    """Migrate legacy crawled data into semantic_observations (reshape, not re-crawl)."""
    from fd_open_data_mcp.crawl.migrate import (
        migrate_astock_daily, migrate_stock_daily, migrate_financials,
    )
    from fd_open_data_mcp.db import get_database

    s = get_database().get_session()
    try:
        if source == "astock":
            _echo(migrate_astock_daily(s, symbol=symbol, limit=limit))
        elif source == "astock-hk":
            _echo(migrate_stock_daily(s, "astock_hk_daily", symbol=symbol, limit=limit))
        elif source == "astock-us":
            _echo(migrate_stock_daily(s, "astock_us_daily", symbol=symbol, limit=limit))
        elif source in ("astock-balance", "astock-profit", "astock-cashflow"):
            table = {"astock-balance": "astock_balance_sheet",
                     "astock-profit": "astock_profit_sheet",
                     "astock-cashflow": "astock_cash_flow"}[source]
            _echo(migrate_financials(s, table, symbol=symbol, limit=limit))
    finally:
        s.close()


@cli.command("list-cnreport-rules")
@click.option("--indicator", default=None)
@click.option("--document-type", default=None)
@click.option("--module", default=None)
@click.option("--kind", default=None)
@click.option("--limit", type=int, default=100)
def list_cnreport_rules_cmd(indicator, document_type, module, kind, limit):
    """Browse cn-report's extraction rules (llm_rules + script_rules)."""
    from fd_open_data_mcp.catalog.cnreport_rules import read_cnreport_rules

    rules, errors = read_cnreport_rules(None, indicator, document_type, module, kind, limit)
    _echo({"count": len(rules), "rules": rules, "errors": errors})


@cli.command("enumerate-wbgapi-indicators")
@click.option("--db", default=None)
def enumerate_wbgapi_indicators_cmd(db):
    """Enumerate all WDI indicators into columns + concepts + bindings."""
    from fd_open_data_mcp.catalog.wbgapi_enumerate import enumerate_wbgapi_indicators as _enum
    from fd_open_data_mcp.db import get_database

    s = get_database().get_session()
    try:
        _echo(_enum(s, db))
    finally:
        s.close()


@cli.command("register-datasource")
@click.argument("path")
def register_datasource_cmd(path):
    """Load a manifest (YAML/JSON/Python) and register it as a datasource."""
    from fd_open_data_protocol.loader import load_catalog

    from fd_open_data_mcp.catalog.register import register_datasource as _register
    from fd_open_data_mcp.db import get_database

    manifest = load_catalog(path)
    s = get_database().get_session()
    try:
        _echo(_register(manifest, s))
    finally:
        s.close()


@cli.command("register-discovered")
def register_discovered_cmd():
    """Auto-discover + register manifests from entry points + a datasources/ dir."""
    from fd_open_data_mcp.catalog.register import discover_datasources
    from fd_open_data_mcp.db import get_database

    s = get_database().get_session()
    try:
        _echo({"registered": discover_datasources(s)})
    finally:
        s.close()


@cli.command("seed-proxy-health")
def seed_proxy_health_cmd():
    """Seed proxy pool, ban-rules, and rate-limits with sensible defaults."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.proxy.seed import seed_all

    s = get_database().get_session()
    try:
        _echo(seed_all(s))
    finally:
        s.close()


@cli.command("probe-cycle")
def probe_cycle_cmd():
    """Run one proxy-health probe cycle (recover OPEN circuits, retire burned)."""
    from fd_open_data_mcp.probe.job import run_probe_cycle

    _echo(run_probe_cycle())


@cli.command("proxy-health")
@click.option("--outcomes", type=int, default=5, help="recent outcomes per source")
def proxy_health_cmd(outcomes):
    """Snapshot proxy-pool + circuit states + recent outcomes (operational view).

    Surfaces ``alert: sources_all_proxies_open`` when a source has no healthy
    proxy - ops should add/rotate proxy IPs for that source."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import Proxy
    from fd_open_data_mcp.proxy import circuit

    s = get_database().get_session()
    try:
        proxies = [
            {"id": p.id, "scheme": p.scheme, "ip": p.ip,
             "status": p.status, "label": p.label}
            for p in s.query(Proxy).all()
        ]
    finally:
        s.close()
    circuits = circuit.all_circuits()
    outcome_snap = {}
    for c in circuits:
        outcome_snap[c["source"]] = circuit.recent_outcomes(c["source"], outcomes)
    alert_sources = circuit.sources_all_proxies_open(None)
    _echo({
        "proxies": proxies,
        "circuits": circuits,
        "recent_outcomes": outcome_snap,
        "alert": {"sources_all_proxies_open": alert_sources},
    })


if __name__ == "__main__":
    cli()
