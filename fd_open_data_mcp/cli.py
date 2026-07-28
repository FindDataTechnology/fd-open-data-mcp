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


if __name__ == "__main__":
    cli()
