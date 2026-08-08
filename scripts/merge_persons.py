#!/usr/bin/env python3
"""Merge two `person` entities into one (add-fund-crawl-control-center, task 2.5).

Escape hatch for the (姓名, 所属公司) identity heuristic (design D2):
- 跳槽: same human, two entities under old/new company names.
- 重名 within one company (rare): two entities wrongly created.

Everything pointing at the loser is repointed to the winner, then the loser
is deleted:
- entity_relationships (source_id / target_id) — repointed; on collision
  (winner already has an equivalent edge) the loser edge is dropped.
- entity_source_identifiers — repointed; on source collision the loser's
  identifier is recorded in winner metadata_json.merged_identifiers instead.
- semantic_observations — repointed; on (concept, date) collision the loser's
  row is dropped (winner's observation wins).
- metadata_json — winner fields win; current_fund_codes/_names are unioned;
  merged_from records the loser code(s).

Usage:
    python scripts/merge_persons.py '张三@旧公司' '张三@新公司'          # 1st arg survives
    python scripts/merge_persons.py '张三@旧公司' '张三@新公司' --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("winner", help="surviving person code, e.g. '张三@新公司'")
    ap.add_argument("loser", help="person code to absorb and delete")
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eng = create_engine(args.db_url)
    with eng.connect() as conn:
        w = conn.execute(text(
            "SELECT id, metadata_json FROM entities WHERE entity_type='person' AND code=:c"),
            {"c": args.winner}).first()
        l = conn.execute(text(
            "SELECT id, metadata_json FROM entities WHERE entity_type='person' AND code=:c"),
            {"c": args.loser}).first()
        if w is None or l is None:
            sys.exit(f"not found: {'winner ' + args.winner if w is None else ''}"
                     f"{' loser ' + args.loser if l is None else ''}")
        wid, lid = w.id, l.id
        print(f"winner: {args.winner} (id={wid}); loser: {args.loser} (id={lid})")

        # --- edges: repoint, dropping collisions
        for col in ("source_id", "target_id"):
            rows = conn.execute(text(
                f"SELECT id, source_id, relation_type, target_id, valid_from FROM entity_relationships WHERE {col}=:lid"),
                {"lid": lid}).fetchall()
            n_repoint = n_drop = 0
            for r in rows:
                new_src, new_tgt = (wid, r.target_id) if col == "source_id" else (r.source_id, wid)
                dup = conn.execute(text(
                    "SELECT 1 FROM entity_relationships WHERE source_id=:s AND relation_type=:rt"
                    " AND target_id=:t AND valid_from IS NOT DISTINCT FROM :vf AND id<>:id"),
                    {"s": new_src, "rt": r.relation_type, "t": new_tgt, "vf": r.valid_from, "id": r.id}).first()
                if dup:
                    conn.execute(text("DELETE FROM entity_relationships WHERE id=:id"), {"id": r.id})
                    n_drop += 1
                else:
                    conn.execute(text(f"UPDATE entity_relationships SET {col}=:wid WHERE id=:id"),
                                 {"wid": wid, "id": r.id})
                    n_repoint += 1
            print(f"  edges ({col}): {n_repoint} repointed, {n_drop} dropped (collision)")

        # --- identifiers: repoint, record collisions in metadata
        meta = dict(w.metadata_json or {})
        merged_ids = list(meta.get("merged_identifiers") or [])
        for r in conn.execute(text(
                "SELECT id, source, identifier FROM entity_source_identifiers"
                " WHERE entity_type='person' AND entity_id=:lid"), {"lid": lid}).fetchall():
            dup = conn.execute(text(
                "SELECT 1 FROM entity_source_identifiers WHERE entity_type='person'"
                " AND entity_id=:wid AND source=:src"), {"wid": wid, "src": r.source}).first()
            if dup:
                conn.execute(text("DELETE FROM entity_source_identifiers WHERE id=:id"), {"id": r.id})
                merged_ids.append({"source": r.source, "identifier": r.identifier,
                                   "from": args.loser})
                print(f"  identifier {r.source}={r.identifier}: collision, recorded in metadata")
            else:
                conn.execute(text("UPDATE entity_source_identifiers SET entity_id=:wid WHERE id=:id"),
                             {"wid": wid, "id": r.id})
                print(f"  identifier {r.source}={r.identifier}: repointed")

        # --- observations: repoint, drop collisions (winner's value wins)
        n_obs = n_obs_drop = 0
        for r in conn.execute(text(
                "SELECT id, concept_id, date FROM semantic_observations"
                " WHERE entity_type='person' AND entity_id=:lid"), {"lid": lid}).fetchall():
            dup = conn.execute(text(
                "SELECT 1 FROM semantic_observations WHERE concept_id=:cid AND entity_type='person'"
                " AND entity_id=:wid AND date=:d"), {"cid": r.concept_id, "wid": wid, "d": r.date}).first()
            if dup:
                conn.execute(text("DELETE FROM semantic_observations WHERE id=:id"), {"id": r.id})
                n_obs_drop += 1
            else:
                conn.execute(text(
                    "UPDATE semantic_observations SET entity_id=:wid WHERE id=:id"),
                    {"wid": wid, "id": r.id})
                n_obs += 1
        print(f"  observations: {n_obs} repointed, {n_obs_drop} dropped (collision)")

        # --- metadata: winner wins; union fund lists; record provenance
        lmeta = dict(l.metadata_json or {})
        for key in ("current_fund_codes", "current_fund_names"):
            union = list(dict.fromkeys((meta.get(key) or []) + (lmeta.get(key) or [])))
            if union:
                meta[key] = union
        meta["merged_from"] = list(dict.fromkeys((meta.get("merged_from") or []) + [args.loser]))
        if merged_ids:
            meta["merged_identifiers"] = merged_ids
        conn.execute(text(
            "UPDATE entities SET metadata_json=CAST(:m AS jsonb), updated_at=now() WHERE id=:wid"),
            {"m": json.dumps(meta, ensure_ascii=False), "wid": wid})

        conn.execute(text("DELETE FROM entities WHERE id=:lid AND entity_type='person'"), {"lid": lid})
        print(f"  loser entity {lid} deleted; metadata merged")

        if args.dry_run:
            conn.rollback()
            print("dry-run: rolled back")
        else:
            conn.commit()
            print("done: committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
