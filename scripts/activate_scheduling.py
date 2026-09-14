#!/usr/bin/env python3
"""Binding assistant for the schedule-activation OpenSpec change.

Enumerates the registered akshare (24 fns) + datacommons (1 fn) functions,
matches their manifest columns against the catalog's EXISTING concept
vocabulary, emits a review report (matched with confidence + rationale,
refused with reasons), and applies the reviewed bindings as upsert-safe rows.

Design constraints honoured (design D1):
- Bind ONLY to concepts that already exist in the target catalog. Nothing is
  invented on demand; a mapping whose concept is absent is refused with a
  reason instead.
- Provenance is tagged ``schedule-activation`` (ConceptBinding.provenance is a
  free-text String(32) with no constraint).

Two manifest defects are corrected at registration time (reported, not hidden):
- akshare commands carry a ``get_`` prefix (``get_stock_zh_a_hist``) while the
  adapter registry registers bare names (``stock_zh_a_hist``) and ``adapter_for``
  is an exact dict lookup -- a prefixed catalog row can never resolve at
  runtime. Commands are normalised to the registry name on registration.

Usage:
    python scripts/activate_scheduling.py --db-url sqlite:////tmp/x.db --reset
    python scripts/activate_scheduling.py --report-only --output-report r.md
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, "/Users/chengsishi/finddata/fd-open-data-protocol")
from fd_open_data_protocol.loader import load_catalog

from fd_open_data_mcp.models import (
    Base,
    Concept,
    ConceptBinding,
    Function,
    FunctionColumn,
    Source,
)

PROVENANCE = "schedule-activation"

# The catalog mirror: holds the real concept vocabulary (ids 233-244 for stock,
# unit='currency'/'shares', financials frequency='yearly') and the binding
# precedents. Gitignored local artifact, so it is copied, never written in place.
CANONICAL_DB = Path(
    "/Users/chengsishi/finddata/fd-open-data-mcp/fd_open_data_mcp/metadata/daas.db"
)
MANIFEST_DIR = Path("/Users/chengsishi/finddata/fd-industry-data/manifests")
DEFAULT_URL = "sqlite:////tmp/schedule_activation_trial.db"


# ---------------------------------------------------------------------------
# Column -> concept maps, gated by function family
#
# Column names repeat across families (开盘 is both an A-share and an ETF
# column), so the function decides which vocabulary applies. Gating on the
# command is what prevents a fund column from binding to a stock concept.
# ---------------------------------------------------------------------------

STOCK_PRICE_COLS: dict[str, str] = {
    "开盘": "price.open", "收盘": "price.close", "最高": "price.high",
    "最低": "price.low", "成交量": "price.volume", "成交额": "price.amount",
    "open": "price.open", "close": "price.close", "high": "price.high",
    "low": "price.low", "volume": "price.volume", "amount": "price.amount",
}

STOCK_FIN_COLS: dict[str, str] = {
    "营业总收入": "financials.revenue",
    "净利润": "financials.net_income",
    "资产总计": "financials.total_assets",
    "负债合计": "financials.total_liabilities",
    "股东权益合计": "financials.equity",
    "经营活动产生的现金流量净额": "financials.operating_cash_flow",
}

FUND_NAV_COLS: dict[str, str] = {
    "单位净值": "nav.unit", "累计净值": "nav.accumulated", "日增长率": "nav.daily_growth",
}

FUND_YIELD_COLS: dict[str, str] = {
    "每万份收益": "yield.per_10k", "7日年化收益率": "yield.7day_annualized",
}

FUND_RETURN_COLS: dict[str, str] = {
    "近1周": "return.1w", "近1月": "return.1m", "近3月": "return.3m",
    "近6月": "return.6m", "近1年": "return.1y", "近3年": "return.3y",
    "成立来": "return.since_inception",
}

FUND_MISC_COLS: dict[str, str] = {
    "最新价": "price.close", "最新规模": "aum", "晨星评级": "rating.stars",
}

PERSON_COLS: dict[str, str] = {
    "累计从业时间": "tenure_days",
    "现任基金资产总规模": "aum_total",
    "现任基金最佳回报": "best_return",
    "现任基金数": "funds_count",
}

# Row keys / dates / status flags -- not measurements.
IDENTIFIER_COLS: set[str] = {
    "date", "日期", "净值日期", "报告期", "股票代码", "证券代码", "基金代码",
    "代码", "序号", "证券简称",
}

# Measurements the catalog has no concept for. Refused rather than widened.
NON_CONCEPT_COLS: set[str] = {
    "振幅", "涨跌幅", "涨跌额", "换手率", "outstanding_share", "turnover",
    "总市值", "流通市值", "本产品最大回撒", "周期收益同类排名",
    "申购状态", "赎回状态", "业绩类型", "周期", "基本每股收益",
    "净资产收益率(%)", "每股收益(元)", "货币资金", "存货",
    "投资活动产生的现金流量净额", "归属于母公司股东的净利润", "证券简称",
    "最新规模",
}

# command (bare) -> which map family it draws from
PERSON_COMMANDS = {"fund_manager_em"}


def family_for(command: str) -> str:
    """Which vocabulary family a (bare) command belongs to."""
    if command in PERSON_COMMANDS:
        return "person"
    if command.startswith("fund_"):
        return "fund"
    return "stock"


def maps_for(command: str) -> list[tuple[str, dict[str, str]]]:
    fam = family_for(command)
    if fam == "person":
        return [("person", PERSON_COLS)]
    if fam == "fund":
        return [
            ("fund-nav", FUND_NAV_COLS),
            ("fund-price", STOCK_PRICE_COLS),
            ("fund-yield", FUND_YIELD_COLS),
            ("fund-return", FUND_RETURN_COLS),
            ("fund-misc", FUND_MISC_COLS),
        ]
    return [("stock-price", STOCK_PRICE_COLS), ("stock-financials", STOCK_FIN_COLS)]


def normalize_command(cmd: str) -> str:
    """Manifest command -> adapter-registry name (the manifest's ``get_`` prefix
    does not exist in ``fd_open_data_mcp.adapters``, where lookups are exact)."""
    return cmd[4:] if cmd.startswith("get_") else cmd


def resolve_concept(column_name: str, command: str) -> tuple[str | None, str | None, str]:
    """(concept_code, entity_type, rationale) for a column, or (None, None, reason)."""
    fam = family_for(command)
    for map_name, cmap in maps_for(command):
        if column_name in cmap:
            code = cmap[column_name]
            return code, fam, (
                f"{map_name} map: '{column_name}' -> {code} "
                f"(entity_type={fam}, via {command})"
            )
    if column_name in IDENTIFIER_COLS:
        return None, None, f"row key / date column, not a measurement: '{column_name}'"
    if column_name in NON_CONCEPT_COLS:
        return None, None, (
            f"no concept in the catalog vocabulary for '{column_name}' "
            f"(refused rather than widened)"
        )
    return None, None, f"no mapping for '{column_name}' from {command}"


@dataclass
class BindingCandidate:
    source: str
    command: str
    column_name: str
    column_type: str | None
    concept_code: str | None
    entity_type: str | None
    concept_id: int | None
    confidence: float
    rationale: str
    refused: bool = False
    refusal_reason: str | None = None


@dataclass
class BindingReport:
    candidates: list[BindingCandidate] = field(default_factory=list)
    matched: int = 0
    refused: int = 0
    errors: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# schedule-activation — binding report", ""]
        lines.append(
            f"Functions examined: {len({(c.source, c.command) for c in self.candidates})} "
            f"(akshare: 24, datacommons: 1)"
        )
        lines.append(f"Matched columns: {self.matched} | Refused columns: {self.refused}")
        lines.append(f"Refused functions: {len({(c.source, c.command) for c in self.candidates if c.refused and all(x.refused for x in self.candidates if x.source == c.source and x.command == c.command)})}")
        lines.append("")
        lines.append("## Matched")
        for c in self.candidates:
            if not c.refused:
                lines.append(
                    f"- `{c.source}.{c.command}.{c.column_name}` -> "
                    f"`{c.entity_type}:{c.concept_code}` (concept id {c.concept_id}, "
                    f"conf={c.confidence:.2f}, provenance={PROVENANCE})  \n"
                    f"  _{c.rationale}_"
                )
        lines.append("")
        lines.append("## Refused")
        for c in self.candidates:
            if c.refused:
                lines.append(
                    f"- `{c.source}.{c.command}.{c.column_name}` — {c.refusal_reason}"
                )
        if self.errors:
            lines.append("")
            lines.append("## Errors")
            lines.extend(f"- {e}" for e in self.errors)
        return "\n".join(lines)


def import_vocabulary(eng) -> int:
    """Copy the concept vocabulary out of the canonical mirror.

    The mirror is a pre-existing snapshot whose schema predates the current
    models, so it is read with plain sqlite3 and only the concept rows are
    carried over -- never the stale tables.
    """
    import sqlite3

    src = sqlite3.connect(f"file:{CANONICAL_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT code, entity_type, measure, unit, frequency, category, "
        "       name_zh, name_en FROM concepts"
    ).fetchall()
    src.close()

    SF = sessionmaker(bind=eng)
    imported = 0
    with SF() as session:
        for r in rows:
            key = dict(
                code=r["code"], entity_type=r["entity_type"],
                measure=r["measure"] or "", unit=r["unit"], frequency=r["frequency"],
            )
            if session.query(Concept).filter_by(**key).first() is None:
                session.add(Concept(
                    **key, category=r["category"] or "",
                    name_zh=r["name_zh"] or "", name_en=r["name_en"] or "",
                    source=None, verified=True,
                ))
                imported += 1
        session.commit()
    print(f"imported {imported} concepts from the canonical mirror")
    return imported


def bootstrap_target(db_url: str, reset: bool) -> None:
    """Materialise the target catalog: a current-schema store carrying the
    canonical concept vocabulary, plus the two manifests registered under their
    runtime (bare) command names."""
    from fd_open_data_mcp.catalog.register import register_datasource

    if reset and db_url.startswith("sqlite:////"):
        target = Path(db_url.replace("sqlite:////", "/"))
        target.unlink(missing_ok=True)

    eng = create_engine(db_url)
    Base.metadata.create_all(eng)
    import_vocabulary(eng)
    SF = sessionmaker(bind=eng)

    akshare = load_catalog(str(MANIFEST_DIR / "akshare.yaml"))
    datacommons = load_catalog(str(MANIFEST_DIR / "datacommons.yaml"))
    for manifest in (akshare, datacommons):
        for fspec in manifest.functions:
            fspec.command = normalize_command(fspec.command)
    with SF() as session:
        register_datasource(akshare, session)
        register_datasource(datacommons, session)


def seed_vocabulary(db_url: str) -> int:
    """Upsert the governed fund/person concepts (scripts/seed_fund_concepts.py).

    The catalog mirror carries the stock-vocabulary half but predates the fund
    work, so the fund/person half is materialised from its in-repo definition.
    """
    sys.path.insert(0, "/Users/chengsishi/finddata/fd-open-data-mcp/scripts")
    from seed_fund_concepts import FUND_CONCEPTS, PERSON_CONCEPTS

    eng = create_engine(db_url)
    SF = sessionmaker(bind=eng)
    created = 0
    with SF() as session:
        for code, entity_type, frequency, unit, category, name_zh, name_en in (
            FUND_CONCEPTS + PERSON_CONCEPTS
        ):
            key = dict(code=code, entity_type=entity_type, measure="", unit=unit,
                       frequency=frequency)
            if session.query(Concept).filter_by(**key).first() is None:
                session.add(Concept(**key, category=category, name_zh=name_zh,
                                    name_en=name_en, source=None, verified=True))
                created += 1
        session.commit()
    if created:
        print(f"seeded {created} governed fund/person concepts")
    return created


def find_concept(session: Session, code: str, entity_type: str) -> Concept | None:
    """The existing catalog concept for (code, entity_type), if any."""
    return (
        session.query(Concept)
        .filter_by(code=code, entity_type=entity_type)
        .first()
    )


def enumerate_candidates(akshare_manifest: Any, datacommons_manifest: Any) -> list[BindingCandidate]:
    candidates: list[BindingCandidate] = []

    for fspec in akshare_manifest.functions:
        command = fspec.command
        for cspec in fspec.columns:
            code, entity_type, rationale = resolve_concept(cspec.name, command)
            refused = code is None
            candidates.append(BindingCandidate(
                source="akshare",
                command=command,
                column_name=cspec.name,
                column_type=cspec.type,
                concept_code=code,
                entity_type=entity_type,
                concept_id=None,
                confidence=1.0 if not refused else 0.0,
                rationale=rationale,
                refused=refused,
                refusal_reason=None if not refused else rationale,
            ))

    # datacommons get_observation exposes only generic date/value columns; a
    # specific concept would need the binding-time variable_dcid, which is not
    # part of the manifest's column list. Refused by design.
    for fspec in datacommons_manifest.functions:
        for cspec in fspec.columns:
            reason = (
                "generic date/value column — no variable-specific concept without a "
                "binding-time variable_dcid"
            )
            candidates.append(BindingCandidate(
                source="datacommons",
                command=fspec.command,
                column_name=cspec.name,
                column_type=cspec.type,
                concept_code=None,
                entity_type=None,
                concept_id=None,
                confidence=0.0,
                rationale=reason,
                refused=True,
                refusal_reason=reason,
            ))

    return candidates


def apply_bindings(
    session: Session, candidates: list[BindingCandidate], dry_run: bool
) -> tuple[int, int, list[str]]:
    """Insert bindings for candidates whose concept exists. Returns
    (added, skipped_existing, problems). Idempotent: re-running adds nothing."""
    added = skipped = 0
    problems: list[str] = []

    for c in candidates:
        if c.refused or not c.concept_code or not c.entity_type:
            continue

        concept = find_concept(session, c.concept_code, c.entity_type)
        if concept is None:
            c.refused = True
            c.refusal_reason = (
                f"concept {c.entity_type}:{c.concept_code} is not in the catalog "
                f"vocabulary (bind-only-to-existing)"
            )
            problems.append(c.refusal_reason)
            continue
        c.concept_id = concept.id

        col = find_function_column(session, c.source, c.command, c.column_name)
        if col is None:
            problems.append(
                f"FunctionColumn not found: {c.source}.{c.command}.{c.column_name}"
            )
            continue

        if session.query(ConceptBinding).filter_by(
            concept_id=concept.id, column_id=col.id
        ).first():
            skipped += 1
            continue

        if not dry_run:
            session.add(ConceptBinding(
                concept_id=concept.id,
                column_id=col.id,
                confidence=c.confidence,
                provenance=PROVENANCE,
                reviewed=True,
            ))
        added += 1

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return added, skipped, problems


def find_function_column(
    session: Session, source: str, command: str, column_name: str
) -> FunctionColumn | None:
    src = session.query(Source).filter_by(name=source).first()
    if not src:
        return None
    fn = session.query(Function).filter_by(source_id=src.id, command=command).first()
    if not fn:
        return None
    return next((c for c in fn.columns if c.name == column_name), None)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db-url", default=os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--reset", action="store_true",
                    help="re-seed the target catalog from the canonical mirror")
    ap.add_argument("--dry-run", action="store_true", help="roll back all changes")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--output-report", type=Path)
    args = ap.parse_args()

    bootstrap_target(args.db_url, args.reset)
    seed_vocabulary(args.db_url)

    akshare = load_catalog(str(MANIFEST_DIR / "akshare.yaml"))
    datacommons = load_catalog(str(MANIFEST_DIR / "datacommons.yaml"))
    for m in (akshare, datacommons):
        for fspec in m.functions:
            fspec.command = normalize_command(fspec.command)

    candidates = enumerate_candidates(akshare, datacommons)

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    with SF() as session:
        added, skipped, problems = apply_bindings(session, candidates, args.dry_run)

    report = BindingReport(candidates=candidates)
    for c in candidates:
        if c.refused:
            report.refused += 1
        else:
            report.matched += 1
    report.errors.extend(problems)

    md = report.to_markdown()
    if args.output_report:
        args.output_report.parent.mkdir(parents=True, exist_ok=True)
        args.output_report.write_text(md)
        print(f"report -> {args.output_report}")
    else:
        print(md)

    print(f"\nbindings: {added} added, {skipped} already present"
          + (" (dry-run, rolled back)" if args.dry_run else ""))
    if problems:
        print(f"problems: {len(problems)}")
        for p in problems[:20]:
            print(f"  - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
