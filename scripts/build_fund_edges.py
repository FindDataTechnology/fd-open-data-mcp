#!/usr/bin/env python3
"""Build fund graph edges (add-fund-crawl-control-center, task 2.4).

Three edge types, all derived from already-seeded entity metadata:

- `managed_by`  fund —managed_by→ person
    From person.metadata_json.current_fund_codes (seeded from
    fund_manager_em). fund_manager_em carries no per-fund tenure dates, so
    valid_from/valid_to stay NULL (ongoing, start unknown); 换帅 history can
    be added later by stamping valid_to and inserting a new edge.
- `issued_by`   fund —issued_by→ organization
    From fund.metadata_json.company. Organization entities are
    find-or-created (code = company name, metadata role='fund_company').
- `tracks`      fund —tracks→ index  (best-effort)
    From fund.metadata_json.benchmark text (e.g. '沪深300指数收益率×80%+中证
    全债指数收益率×20%'). Matches against existing index entities' name_zh
    plus a small alias table of major CN indices (find-or-created with real
    index codes). Benchmarks naming no known index are counted, not edged.

Idempotent: an edge is skipped when (source, relation_type, target,
valid_from) already exists.

Usage:
    python scripts/build_fund_edges.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Entity, EntityRelationship

DEFAULT_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"

# Major CN indices referenced by fund benchmarks: alias -> (code, name_zh).
# Aliases are matched as substrings of the benchmark string, longest first.
INDEX_ALIASES: dict[str, tuple[str, str]] = {
    "沪深300": ("000300", "沪深300指数"),
    "中证500": ("000905", "中证500指数"),
    "中证1000": ("000852", "中证1000指数"),
    "中证800": ("000906", "中证800指数"),
    "中证700": ("000907", "中证700指数"),
    "中证200": ("000904", "中证200指数"),
    "中证100": ("000903", "中证100指数"),
    "上证50": ("000016", "上证50指数"),
    "上证180": ("000010", "上证180指数"),
    "创业板指数": ("399006", "创业板指数"),
    "创业板指": ("399006", "创业板指数"),
    "科创50": ("000688", "上证科创板50成份指数"),
    "中证全债": ("H11001", "中证全债指数"),
    "上证国债": ("000012", "上证国债指数"),
    "中证红利": ("000922", "中证红利指数"),
    "深证成指": ("399001", "深证成份指数"),
    "深证100": ("399330", "深证100指数"),
    "中小100": ("399005", "中小100指数"),
    "恒生中国企业": ("HSCEI", "恒生中国企业指数"),
    "恒生指数": ("HSI", "恒生指数"),
    "恒生科技": ("HSTECH", "恒生科技指数"),
    "标普500": ("SPX", "标普500指数"),
    "纳斯达克100": ("NDX", "纳斯达克100指数"),
    "上证综合指数": ("SHCOMP", "上证综合指数"),
    "上证综指": ("SHCOMP", "上证综合指数"),
}


def _get_or_create(session: Session, entity_type: str, code: str, name_zh: str,
                   metadata: dict, dry_run: bool) -> "Entity | None":
    e = session.query(Entity).filter_by(entity_type=entity_type, code=code).first()
    if e is None:
        e = Entity(entity_type=entity_type, code=code, name_zh=name_zh,
                   metadata_json=metadata,
                   updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        if not dry_run:
            session.add(e)
            session.flush()
    return e


def _edge_exists(session: Session, source_id: int, rel: str, target_id: int) -> bool:
    return session.query(EntityRelationship).filter(
        EntityRelationship.source_id == source_id,
        EntityRelationship.relation_type == rel,
        EntityRelationship.target_id == target_id,
        EntityRelationship.valid_from.is_(None),
    ).first() is not None


def _add_edge(session: Session, source_id: int, rel: str, target_id: int,
              metadata: dict, dry_run: bool, stats: dict) -> None:
    # NOTE: uq_rel_edges treats NULL valid_from as distinct (PG semantics), so
    # dedupe for NULL-valid_from edges must be done here in Python.
    if _edge_exists(session, source_id, rel, target_id):
        stats[f"{rel}_exists"] = stats.get(f"{rel}_exists", 0) + 1
        return
    stats[rel] = stats.get(rel, 0) + 1
    if not dry_run:
        session.add(EntityRelationship(
            source_id=source_id, relation_type=rel, target_id=target_id,
            metadata_json=metadata,
        ))


def _match_index(session: Session, benchmark: str, dry_run: bool) -> "Entity | None":
    """Longest-alias-first match of a benchmark string to an index entity."""
    for alias in sorted(INDEX_ALIASES, key=len, reverse=True):
        if alias in benchmark:
            code, name_zh = INDEX_ALIASES[alias]
            return _get_or_create(session, "index", code, name_zh,
                                  {"seed": "build_fund_edges", "alias": alias}, dry_run)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    asof = datetime.now(timezone.utc).date().isoformat()
    stats: dict[str, int] = {}
    with SF() as session:
        funds = {e.code: e for e in session.query(Entity).filter_by(entity_type="fund")}
        persons = session.query(Entity).filter_by(entity_type="person").all()
        print(f"funds: {len(funds)}, persons: {len(persons)}")

        # --- managed_by: fund -> person (from person.current_fund_codes)
        for p in persons:
            meta = p.metadata_json or {}
            for fc in meta.get("current_fund_codes") or []:
                fund = funds.get(fc)
                if fund is None:
                    stats["managed_by_no_fund"] = stats.get("managed_by_no_fund", 0) + 1
                    continue
                _add_edge(session, fund.id, "managed_by", p.id,
                          {"source": "fund_manager_em", "asof": asof}, args.dry_run, stats)

        # --- issued_by: fund -> organization (from fund.company)
        for fund in funds.values():
            company = (fund.metadata_json or {}).get("company")
            if company:
                org = _get_or_create(session, "organization", company, company,
                                     {"role": "fund_company", "seed": "build_fund_edges"},
                                     args.dry_run)
                if org is not None:
                    _add_edge(session, fund.id, "issued_by", org.id,
                              {"asof": asof}, args.dry_run, stats)

        # --- tracks: fund -> index (from fund.benchmark, best-effort)
        for fund in funds.values():
            benchmark = (fund.metadata_json or {}).get("benchmark")
            if not benchmark:
                stats["tracks_no_benchmark"] = stats.get("tracks_no_benchmark", 0) + 1
                continue
            idx = _match_index(session, benchmark, args.dry_run)
            if idx is None:
                stats["tracks_unmatched"] = stats.get("tracks_unmatched", 0) + 1
                continue
            _add_edge(session, fund.id, "tracks", idx.id,
                      {"benchmark_raw": benchmark, "asof": asof}, args.dry_run, stats)

        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    print("done:", ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
          + (" (dry-run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
