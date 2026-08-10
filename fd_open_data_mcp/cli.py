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


@cli.group("cluster")
def cluster_grp():
    """Manage the multi-cluster worker fleet (add/list worker clusters)."""


@cluster_grp.command("add")
@click.option("--name", required=True, help="Unique cluster name, e.g. vultr-tokyo")
@click.option("--api-server", required=True, help="https://<host>:6443")
@click.option("--namespace", default="scraw", help="namespace crawl Jobs run in")
@click.option("--image", default="harbor.local/lawcraw_business/scraw-fd-open-data-mcp:latest")
@click.option("--tag", "tags", multiple=True,
              help="real_source this cluster can fetch (e.g. eastmoney). Repeatable. Empty = wildcard.")
@click.option("--capacity", type=int, default=4, help="max concurrent open runs")
@click.option("--kubeconfig-secret", help="k8s Secret name holding this cluster's creds")
def cluster_add_cmd(name, api_server, namespace, image, tags, capacity, kubeconfig_secret):
    """Register (or update) a worker cluster. Onboarding step 3 of k8s/master/README.md."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import Cluster

    s = get_database().get_session()
    try:
        existing = s.query(Cluster).filter_by(name=name).first()
        if existing:
            existing.api_server = api_server
            existing.namespace = namespace
            existing.image = image
            existing.tags = list(tags) or None
            existing.capacity = capacity
            existing.kubeconfig_secret = kubeconfig_secret
            existing.enabled = True
        else:
            s.add(Cluster(name=name, api_server=api_server, namespace=namespace,
                          image=image, tags=list(tags) or None, capacity=capacity,
                          kubeconfig_secret=kubeconfig_secret))
        s.commit()
        row = s.query(Cluster).filter_by(name=name).first()
        _echo(row.toDict())
    finally:
        s.close()


@cluster_grp.command("list")
def cluster_list_cmd():
    """List registered worker clusters."""
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.models import Cluster

    s = get_database().get_session()
    try:
        rows = s.query(Cluster).order_by(Cluster.id).all()
        click.echo(f"{len(rows)} cluster(s):")
        for r in rows:
            click.echo(f"  [{r.id}] {r.name}  enabled={r.enabled}  tags={r.tags}  "
                       f"capacity={r.capacity}  api={r.api_server}")
    finally:
        s.close()


@cli.command("migrate-astock-daily")
@click.option("--symbol", "symbols", multiple=True,
              help="Restrict to one or more 6-digit symbols (default: all astock_daily).")
def migrate_astock_daily_cmd(symbols):
    """Bulk-migrate astock_daily OHLCV into semantic_observations.

    Idempotent (ON CONFLICT DO NOTHING). Backfills stocks missing from the
    semantic layer (e.g. 中国银行 601988, 平安银行 000001) from the legacy
    astock_daily table into System-B stock concepts (price.open/close/high/low,
    price.volume, price.amount).
    """
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.migrate import migrate_astock_daily

    s = get_database().get_session()
    try:
        _echo(migrate_astock_daily(s, symbols=list(symbols) or None))
    finally:
        s.close()


@cli.command("serve")
def serve_cmd():
    """Run the FastMCP server (stdio transport)."""
    from fd_open_data_mcp.server import main

    main()


@cli.command("panel")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
def panel_cmd(host, port):
    """Serve the crawl control-center panel (add-fund-crawl-control-center)."""
    import uvicorn

    from fd_open_data_mcp.panel.app import app

    uvicorn.run(app, host=host, port=port)


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


@cli.command("repair-stock-identifiers")
@click.option("--apply", is_flag=True, default=False,
              help="Write canonical identifiers (default: dry-run report only).")
def repair_stock_identifiers_cmd(apply):
    """Report/fix stock source-identifier drift.

    A stock's akshare identifier must equal its 6-digit code; yfinance must be
    the code with the .SS/.SZ/.HK suffix. Drift (e.g. 中国银行 -> akshare "02211"
    instead of "601988") breaks fetch() ranked dispatch. Dry-run lists drift;
    --apply re-seeds canonical akshare + yfinance for all stock entities
    (cn-report identifiers are never touched).
    """
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.entities.resolver import repair_stock_identifiers

    s = get_database().get_session()
    try:
        _echo(repair_stock_identifiers(s, apply=apply))
    finally:
        s.close()


@cli.command("deprecation-map")
def deprecation_map_cmd():
    """Print the deprecated -> canonical concept alias record.

    Lists every deprecated concept (e.g. PRICE_CLOSE) and its canonical
    replacement (e.g. price.close) so callers can migrate off deprecated
    symbol stock-concept codes.
    """
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.entities.resolver import deprecation_map

    s = get_database().get_session()
    try:
        _echo(deprecation_map(s))
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
@click.option("--start", default=None, help="Date range start (e.g. 2024-01-01). Optional if --since-last.")
@click.option("--end", default=None, help="Date range end (e.g. 2024-12-31). Defaults to today.")
@click.option("--frequency", default=None, help="Cadence hint for the executor.")
@click.option("--since-last", is_flag=True, default=False,
              help="Derive start from max(date) already in semantic_observations (incremental).")
@click.option("--source", "sources", multiple=True,
              help="Restrict to specific source (repeatable). E.g., --source cn-report.")
@click.option("--out", "-o", default=None, help="Write the plan to this file (JSON).")
def plan_crawl_cmd(concept_ids, entity_type, entity_ids, start, end, frequency, since_last, sources, out):
    """Plan a concept crawl -> CrawlPlan (concepts in, methods out)."""
    import datetime as dt
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl as _plan
    from fd_open_data_mcp.db import get_database

    # Validation: need --start or --since-last
    if not start and not since_last:
        raise click.UsageError("Either --start or --since-last is required")
    # Default end to today
    if not end:
        end = dt.date.today().isoformat()
    # If both --start and --since-last, --start wins (since_last ignored)
    if start and since_last:
        since_last = False

    s = get_database().get_session()
    try:
        plan = _plan(
            s, list(concept_ids),
            EntityScope(entity_type=entity_type, entity_ids=list(entity_ids) or None),
            DateRange(start=start, end=end, frequency=frequency),
            since_last=since_last,
            source_filter=list(sources) or None,
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


@cli.command("list-sources")
def list_sources_cmd():
    """List all available data sources and their integration status."""
    from pathlib import Path
    
    # Define all known data sources with descriptions
    ALL_SOURCES = {
        "akshare": ("✅ Full support", "A 股股票、基金等金融数据"),
        "yfinance": ("✅ Full support", "Yahoo Finance 全球股票数据"),
        "cn-report": ("✅ Full support", "中国财务报告提取 (26 个工具)"),
        "nbs-gdp": ("✅ Full support", "国家统计局 GDP 数据"),
        "cisa-industry": ("✅ Full support", "工信部行业统计"),
        "amac-fund": ("✅ Full support", "基金业协会 AMAC 数据"),
        "shfe-metal-futures": ("✅ Full support", "上海期货交易所金属期货"),
        "agriculture": ("✅ Full support", "大连商品交易所农产品期货"),
        "cme-agricultural-futures": ("✅ Full support", "CME 农产品期货"),
        "chemicals": ("✅ Full support", "化工产品价格与 PMI"),
        "electronics": ("✅ Full support", "电子信息产业协会数据"),
        "nonferrous": ("✅ Full support", "有色金属产业数据"),
        "flowers-kifc": ("✅ Full support", "昆明国际花卉拍卖中心"),
        "fin_platforms": ("✅ Full support", "Wind 金融终端数据"),
        "sac-securities": ("✅ Full support", "证券业协会交易统计"),
        "edgar": ("✅ Full support", "SEC EDGAR - requires EDGAR_IDENTITY env var"),
        "wbgapi": ("✅ Full support", "World Bank data API"),
        "cn-gov": ("⚠️  Read-only registry", "China government open information"),
        "world": ("⚠️  Read-only catalog", "CKAN + Chinese NBS Statistics"),
    }
    
    # Check which adapters exist and are registered
    adapter_dir = Path(__file__).parent / "adapters"
    existing_adapters = set()
    for f in adapter_dir.glob("*.py"):
        if f.name != "__init__.py" and not f.name.startswith("_"):
            existing_adapters.add(f.stem.replace("-", "_"))
    
    # Determine status for each source
    results = []
    for source, (status, description) in sorted(ALL_SOURCES.items()):
        # Normalize adapter name (replace hyphens with underscores)
        adapter_name = source.replace("-", "_")
        
        # Check if adapter exists
        if adapter_name in existing_adapters:
            existing = "✅ Adapter file found"
        else:
            existing = "❌ No adapter file"
        
        results.append({
            "source": source,
            "status": status,
            "description": description,
            "adapter_file": existing,
        })
    
    # Print formatted output
    print("\n📊 Data Sources Available")
    print("=" * 80)
    print(f"{'Source':<25} {'Status':<25} {'Description'}")
    print("-" * 80)
    
    for r in results:
        print(f"{r['source']:<25} {r['status']:<25} {r['description']}")
    
    print("-" * 80)
    print(f"\nTotal: {len(results)} data sources")
    print(f"Fully integrated: {sum(1 for r in results if 'Full support' in r['status'])}")
    
    # Return JSON for programmatic use
    _echo({"sources": results})


if __name__ == "__main__":
    cli()
