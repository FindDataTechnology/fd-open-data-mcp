#!/usr/bin/env python3
"""Seed fund-manager `person` entities (add-fund-crawl-control-center, D2).

Source: akshare `fund_manager_em` — one row per (manager × current fund),
~35k rows collapsing to ~4.3k distinct managers on the composite identity key
`(姓名, 所属公司)` (design D2: no stable upstream code exists).

Per distinct manager:
- `entities` row: entity_type='person', code='姓名@所属公司',
  name_zh=姓名, metadata_json:
    role='fund_manager', company, tenure_days (累计从业时间),
    aum_total_yi (现任基金资产总规模, 亿), best_return_pct (现任基金最佳回报, %),
    current_fund_codes / current_fund_names (现任基金代码/现任基金 lists),
    asof, seed='seed_fund_managers'.
- `entity_source_identifiers` row: source='eastmoney', identifier=序号
  (per-snapshot hint only — NOT a stable key; 跳槽/重名 merges go through
  scripts/merge_persons.py).

Idempotent: re-running upserts by (entity_type, code) and refreshes metadata.

Usage:
    python scripts/seed_fund_managers.py                 # all managers
    python scripts/seed_fund_managers.py --fund-filter   # only managers of seeded fund entities
    python scripts/seed_fund_managers.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Entity, EntitySourceIdentifier

DEFAULT_URL = "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # pandas NaN is not valid JSONB


def _fetch_managers(ak) -> "list[dict]":
    """fund_manager_em -> one record per distinct (姓名, 所属公司)."""
    df = ak.fund_manager_em()
    persons: dict[tuple[str, str], dict] = {}
    for r in df.to_dict("records"):
        name = str(r.get("姓名") or "").strip()
        company = str(r.get("所属公司") or "").strip()
        if not name or not company:
            continue
        key = (name, company)
        fund_code = str(r.get("现任基金代码") or "").strip()
        fund_name = str(r.get("现任基金") or "").strip()
        p = persons.get(key)
        if p is None:
            p = persons[key] = {
                "name": name,
                "company": company,
                "em_seq": str(r.get("序号") or "").strip() or None,
                "tenure_days": _num(r.get("累计从业时间")),
                "aum_total_yi": _num(r.get("现任基金资产总规模")),
                "best_return_pct": _num(r.get("现任基金最佳回报")),
                "fund_codes": [],
                "fund_names": [],
            }
        if fund_code and fund_code not in p["fund_codes"]:
            p["fund_codes"].append(fund_code)
            p["fund_names"].append(fund_name)
        if p["em_seq"] is None and str(r.get("序号") or "").strip():
            p["em_seq"] = str(r["序号"]).strip()
    return list(persons.values())


def _upsert_person(session: Session, p: dict, asof: str, dry_run: bool) -> str:
    code = f"{p['name']}@{p['company']}"
    existing = session.query(Entity).filter_by(entity_type="person", code=code).first()
    metadata = dict(existing.metadata_json or {}) if existing else {}
    metadata.update({
        "role": "fund_manager",
        "company": p["company"],
        "tenure_days": p["tenure_days"],
        "aum_total_yi": p["aum_total_yi"],
        "best_return_pct": p["best_return_pct"],
        "current_fund_codes": p["fund_codes"],
        "current_fund_names": p["fund_names"],
        "asof": asof,
        "seed": "seed_fund_managers",
    })
    metadata = {k: v for k, v in metadata.items() if v is not None}
    if existing:
        existing.name_zh = p["name"]
        existing.metadata_json = metadata
        existing.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        entity, status = existing, "updated"
    else:
        entity = Entity(
            entity_type="person", code=code, name_zh=p["name"],
            metadata_json=metadata, updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        if not dry_run:
            session.add(entity)
            session.flush()
        status = "created"
    if not dry_run and p["em_seq"]:
        # Mirror the 序号 under BOTH 'eastmoney' (the real source) and 'akshare'
        # (the catalog source of fund_manager_em): dispatch resolves identifiers
        # by the function's catalog source, so the akshare row is what makes
        # fund_manager_em routable for person entities.
        for source in ("eastmoney", "akshare"):
            ident = session.query(EntitySourceIdentifier).filter_by(
                entity_type="person", entity_id=entity.id, source=source,
            ).first()
            if ident is None:
                session.add(EntitySourceIdentifier(
                    entity_type="person", entity_id=entity.id,
                    source=source, identifier=p["em_seq"],
                ))
            elif ident.identifier != p["em_seq"]:
                ident.identifier = p["em_seq"]
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fund-filter", action="store_true",
                    help="only seed managers whose 现任基金代码 intersect seeded fund entities")
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        import akshare as ak
    except ImportError:
        sys.exit("akshare not installed: pip install 'fd-open-data-mcp[data]'")

    print("[1/2] fund_manager_em ...")
    persons = _fetch_managers(ak)
    print(f"      {len(persons)} distinct managers on (姓名, 所属公司)")

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    asof = datetime.now(timezone.utc).date().isoformat()
    stats = {"created": 0, "updated": 0}
    with SF() as session:
        if args.fund_filter:
            known = {c for (c,) in session.query(Entity.code).filter_by(entity_type="fund")}
            before = len(persons)
            persons = [p for p in persons if any(c in known for c in p["fund_codes"])]
            print(f"      --fund-filter: {before} -> {len(persons)} (intersect {len(known)} seeded funds)")
        print("[2/2] upsert persons ...")
        for p in persons:
            stats[_upsert_person(session, p, asof, args.dry_run)] += 1
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    print(f"done: {stats['created']} created, {stats['updated']} updated"
          + (" (dry-run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
