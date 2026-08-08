#!/usr/bin/env python3
"""Seed the fund entity universe (add-fund-crawl-control-center, D8).

Builds the canonical `fund` entity set from akshare's `fund_name_em` universe,
ranked by AUM (最新规模), and upserts top-N funds into the ontology DB:

- `entities` rows (entity_type='fund', code=基金代码) with metadata_json:
  subtype (open/etf/lof/money/graded), fund_type (raw 基金类型), company,
  custodian, inception_date, benchmark, rating, aum_yi, aum_asof, managers.
- `entity_source_identifiers` rows (source='akshare', identifier=基金代码).

AUM strategy (bulk per-fund scale is not available upstream — verified
2026-08-08: fund_open_fund_rank_em / fund_exchange_rank_em carry no 规模
column):
- open/LOF funds: per-fund `fund_individual_basic_info_xq` sweep (concurrent,
  checkpointed, resumable). This doubles as the metadata-enrichment pass.
- ETFs: bulk `fund_etf_spot_em` 总市值 as the AUM figure (xueqiu basic-info
  does not cover exchange-listed ETFs or money funds — KeyError 'data').
- money funds: no bulk or per-fund scale source; aum_yi stays None and they
  rank after AUM-known funds.
- `--from-rank` fallback (no AUM at all): seed in fund_open_fund_rank_em
  order with a loud warning. Only for networks where xueqiu/eastmoney-spot
  are unreachable.

Idempotent: re-running upserts by (entity_type, code) / (entity_type,
entity_id, source) and refreshes metadata.

Usage:
    python scripts/seed_fund_universe.py --top 500              # full sweep + upsert
    python scripts/seed_fund_universe.py --top 10 --limit 200   # smoke: sweep 200, seed 10
    python scripts/seed_fund_universe.py --from-rank --top 500  # no-AUM fallback

Run from a network where xueqiu/eastmoney are reachable (NOT the k8s cluster).
Requires akshare: pip install 'fd-open-data-mcp[data]'.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Entity, EntitySourceIdentifier

DEFAULT_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"
CHECKPOINT = Path.home() / ".cache" / "fd_open_data_mcp" / "fund_seed_checkpoint.jsonl"

# design D1 subtype vocabulary
SUBTYPES = ("open", "etf", "lof", "money", "graded")


# --------------------------------------------------------------------------- helpers

def _parse_aum_yi(raw) -> float | None:
    """Parse xueqiu 最新规模 strings like '197.40亿' / '1.23万亿' into 亿 units."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or s in ("---", "--", "暂无", "<NA>"):
        return None
    m = re.match(r"^([0-9.]+)\s*(万亿|亿|万)?$", s)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2) or "亿"
    if unit == "万亿":
        v *= 10000.0
    elif unit == "万":
        v /= 10000.0
    return v


def _classify_subtype(code: str, name: str, fund_type: str, etf_codes: set[str]) -> str:
    """Map to the D1 subtype vocabulary. Raw 基金类型 is kept separately."""
    ft = fund_type or ""
    nm = name or ""
    if "货币" in ft:
        return "money"
    if code in etf_codes or ("ETF" in nm.upper() and "联接" not in nm):
        return "etf"
    if "LOF" in nm.upper():
        return "lof"
    if "分级" in nm or "分级" in ft:
        return "graded"
    return "open"


def _load_checkpoint(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out[d["code"]] = d
            except Exception:  # noqa: BLE001 - skip corrupt lines
                continue
    return out


def _append_checkpoint(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- upstream probes

def _fetch_universe(ak) -> "list[dict]":
    """fund_name_em -> [{code, name, fund_type}], 后端 mirrors dropped."""
    df = ak.fund_name_em()
    out = []
    for r in df.to_dict("records"):
        name = str(r.get("基金简称") or "").strip()
        code = str(r.get("基金代码") or "").strip()
        if not code or name.endswith("(后端)"):
            continue  # 后端 codes mirror the front-end share class
        out.append({"code": code, "name": name, "fund_type": str(r.get("基金类型") or "").strip()})
    return out


def _fetch_etf_spot(ak) -> dict[str, float]:
    """fund_etf_spot_em -> {code: 总市值 in 亿}; also serves as the ETF code set."""
    df = ak.fund_etf_spot_em()
    out = {}
    for r in df.to_dict("records"):
        code = str(r.get("代码") or "").strip()
        cap = r.get("总市值")
        try:
            cap_yi = round(float(cap) / 1e8, 4) if cap is not None else None
            out[code] = None if cap_yi is None or cap_yi != cap_yi else cap_yi  # NaN is not valid JSONB
        except (TypeError, ValueError):
            out[code] = None
    return out


def _xq_basic_info(ak, code: str) -> dict | None:
    """One xueqiu basic-info call -> normalized dict, or None when uncovered."""
    try:
        df = ak.fund_individual_basic_info_xq(symbol=code)
    except Exception:  # noqa: BLE001 - KeyError 'data' for ETF/money, network errors
        return None
    d = {str(i).strip(): v for i, v in zip(df["item"], df["value"])}
    def _s(k):
        v = d.get(k)
        if v is None:
            return None
        s = str(v).strip()
        return None if s in ("", "<NA>", "暂无评级", "---") else s
    return {
        "code": code,
        "name_zh": _s("基金名称"),
        "fullname": _s("基金全称"),
        "inception_date": _s("成立时间"),
        "aum_yi": _parse_aum_yi(d.get("最新规模")),
        "company": _s("基金公司"),
        "managers": _s("基金经理"),
        "custodian": _s("托管银行"),
        "fund_type_xq": _s("基金类型"),
        "rating": _s("基金评级"),
        "benchmark": _s("业绩比较基准"),
    }


def _fetch_fees(ak, code: str) -> dict | None:
    """Best-effort fund_fee_em capture (申购/赎回费率). Sparse upstream:
    申购费率 is empty for many funds; only non-empty frames are kept."""
    fees: dict = {}
    for ind, key in (("申购费率", "subscribe"), ("赎回费率", "redeem")):
        try:
            df = ak.fund_fee_em(symbol=code, indicator=ind)
        except Exception:  # noqa: BLE001
            continue
        if df is not None and len(df):
            fees[key] = [{str(k): str(v) for k, v in row.items()} for row in df.to_dict("records")]
    return fees or None


# --------------------------------------------------------------------------- DB upserts

def _upsert_fund(session: Session, rec: dict, dry_run: bool) -> tuple[str, int | None]:
    """Insert or refresh one fund entity + its akshare identifier."""
    existing = session.query(Entity).filter_by(entity_type="fund", code=rec["code"]).first()
    metadata = dict(existing.metadata_json or {}) if existing else {}
    metadata.update({k: v for k, v in rec["metadata"].items() if v is not None})
    if existing:
        existing.name_zh = rec["name"] or existing.name_zh
        existing.metadata_json = metadata
        existing.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        entity, status = existing, "updated"
    else:
        entity = Entity(
            entity_type="fund", code=rec["code"], name_zh=rec["name"],
            metadata_json=metadata, updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        if not dry_run:
            session.add(entity)
            session.flush()
        status = "created"
    if not dry_run:
        ident = session.query(EntitySourceIdentifier).filter_by(
            entity_type="fund", entity_id=entity.id, source="akshare",
        ).first()
        if ident is None:
            session.add(EntitySourceIdentifier(
                entity_type="fund", entity_id=entity.id, source="akshare", identifier=rec["code"],
            ))
        elif ident.identifier != rec["code"]:
            ident.identifier = rec["code"]
    return status, (None if dry_run else entity.id)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=500, help="seed top-N funds by AUM (default 500)")
    ap.add_argument("--limit", type=int, default=None, help="only sweep the first N universe funds (testing)")
    ap.add_argument("--workers", type=int, default=4, help="xueqiu sweep concurrency (default 4)")
    ap.add_argument("--from-rank", action="store_true", help="fallback: no AUM, use fund_open_fund_rank_em order")
    ap.add_argument("--checkpoint", type=Path, default=CHECKPOINT, help="xueqiu sweep checkpoint JSONL")
    ap.add_argument("--fees", choices=("none", "sample", "all"), default="sample",
                    help="fund_fee_em capture for picked funds: none / first 20 (sample) / all")
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        import akshare as ak
    except ImportError:
        sys.exit("akshare not installed: pip install 'fd-open-data-mcp[data]'")

    print("[1/4] universe: fund_name_em ...")
    universe = _fetch_universe(ak)
    if args.limit:
        universe = universe[: args.limit]
    print(f"      {len(universe)} funds (后端 mirrors dropped)")

    etf_cap: dict[str, float] = {}
    if not args.from_rank:
        print("[2/4] ETF spot (code set + 总市值) ...")
        try:
            etf_cap = _fetch_etf_spot(ak)
            print(f"      {len(etf_cap)} ETFs")
        except Exception as e:  # noqa: BLE001
            print(f"      WARN fund_etf_spot_em failed: {e}; ETF AUM unavailable")

        print(f"[3/4] xueqiu sweep (workers={args.workers}, checkpoint={args.checkpoint}) ...")
        done = _load_checkpoint(args.checkpoint)
        targets = [f for f in universe if f["code"] not in done and f["code"] not in etf_cap
                   and "货币" not in f["fund_type"]]
        print(f"      {len(done)} cached, {len(targets)} to fetch")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(_xq_basic_info, ak, f["code"]): f["code"] for f in targets}
            n = 0
            for fut in as_completed(futs):
                rec = fut.result()
                code = futs[fut]
                _append_checkpoint(args.checkpoint, rec if rec else {"code": code, "uncovered": True})
                done[code] = rec or {"code": code, "uncovered": True}
                n += 1
                if n % 500 == 0:
                    dt = time.time() - t0
                    print(f"      {n}/{len(targets)} fetched ({dt:.0f}s, ~{dt/n*1000:.0f}ms/call)")
        info = done
    else:
        print("[2/4]+[3/4] SKIPPED (--from-rank): AUM disabled, rank order is fund_open_fund_rank_em")
        rank = ak.fund_open_fund_rank_em(symbol="全部")
        order = [str(c).strip() for c in rank["基金代码"].tolist()]
        pos = {c: i for i, c in enumerate(order)}
        universe.sort(key=lambda f: pos.get(f["code"], 1 << 30))
        info = {}

    print("[4/4] classify + rank + upsert ...")
    asof = datetime.now(timezone.utc).date().isoformat()
    for f in universe:
        xq = info.get(f["code"]) or {}
        f["subtype"] = _classify_subtype(f["code"], f["name"], f["fund_type"], set(etf_cap))
        f["xq"] = xq
        if f["subtype"] == "etf":
            f["aum_yi"] = etf_cap.get(f["code"])
        else:
            f["aum_yi"] = xq.get("aum_yi")
    ranked = sorted(universe, key=lambda f: (f["aum_yi"] is None, -(f["aum_yi"] or 0)))
    picked = ranked[: args.top]
    n_with_aum = sum(1 for f in picked if f["aum_yi"] is not None)
    print(f"      picked top {len(picked)} ({n_with_aum} with AUM data); "
          f"AUM range: {next((f['aum_yi'] for f in picked if f['aum_yi']), None)}亿 .. "
          f"{next((f['aum_yi'] for f in reversed(picked) if f['aum_yi']), None)}亿")

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    stats = {"created": 0, "updated": 0}
    with SF() as session:
        for i, f in enumerate(picked):
            xq = f["xq"]
            fees = None
            want_fees = (args.fees == "all") or (args.fees == "sample" and i < 20)
            if not args.from_rank and want_fees:
                fees = _fetch_fees(ak, f["code"])
            rec = {
                "code": f["code"],
                "name": xq.get("name_zh") or f["name"],
                "metadata": {
                    "subtype": f["subtype"],
                    "fund_type": xq.get("fund_type_xq") or f["fund_type"] or None,
                    "company": xq.get("company"),
                    "custodian": xq.get("custodian"),
                    "inception_date": xq.get("inception_date"),
                    "benchmark": xq.get("benchmark"),
                    "rating": xq.get("rating"),
                    "managers": xq.get("managers"),
                    "fees": fees,
                    "aum_yi": f["aum_yi"],
                    "aum_asof": asof if f["aum_yi"] is not None else None,
                    "seed": "seed_fund_universe",
                },
            }
            status, _ = _upsert_fund(session, rec, args.dry_run)
            stats[status] += 1
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    print(f"done: {stats['created']} created, {stats['updated']} updated"
          + (" (dry-run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
