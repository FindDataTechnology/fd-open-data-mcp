#!/usr/bin/env python3
"""Sync 申万 (SW) industry classifications from akshare to entities DB.

This script fetches 申万 L1 industry data and their constituent stocks via akshare,
then upserts them into the entities DB for use in rule filtering.

Usage:
    PYTHONPATH=. python scripts/sync_sw_industries.py

Environment variables:
    FD_OPEN_DATA_MCP_DATABASE_URL - Database URL (default: postgresql://admin:admin123@192.168.1.4:5433/postgres)

Schedule: Run daily or weekly via cron to keep industry assignments fresh.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

# Import db module
sys.path.insert(0, str(Path(__file__).parent.parent))
from fd_open_data_mcp import db as dbmod

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def get_db_session() -> Session:
    """Get database session."""
    db = dbmod.get_database()
    return db.get_session()


def fetch_sw_index_first_info() -> list[dict]:
    """Fetch all 31 申万 L1 industries.

    Returns list of dicts with keys: code, name_zh, component_count
    """
    df = ak.sw_index_first_info()
    rows = []
    for _, row in df.iterrows():
        # Clean code: remove .SI suffix
        code = row['行业代码'].replace('.SI', '')
        rows.append({
            "code": code,
            "name_zh": row['行业名称'],
            "component_count": int(row['成份个数']),
            "pe_ttm": float(row['TTM(滚动)市盈率']) if pd.notna(row['TTM(滚动)市盈率']) else None,
            "pb": float(row['市净率']) if pd.notna(row['市净率']) else None,
        })
    return rows


def fetch_sw_constituents(industry_code: str) -> list[dict]:
    """Fetch constituent stocks for a 申万 L1 industry.

    Args:
        industry_code: 申万 L1 code (e.g., '801780' for 银行)

    Returns:
        List of dicts with keys: stock_code, stock_name, weight, entry_date
    """
    retries = 0
    while retries < MAX_RETRIES:
        try:
            df = ak.index_component_sw(symbol=industry_code)
            result = []
            for _, row in df.iterrows():
                result.append({
                    "stock_code": str(row['证券代码']),
                    "stock_name": row['证券名称'],
                    "weight": float(row['最新权重']) if pd.notna(row['最新权重']) else None,
                    "entry_date": str(row['计入日期']),
                })
            return result
        except Exception as e:
            retries += 1
            if retries == MAX_RETRIES:
                raise
            time.sleep(RETRY_DELAY)


def upsert_entity(session: Session, entity_type: str, code: str, name_en: Optional[str],
                  name_zh: str, metadata: dict) -> int:
    """Upsert an entity record. Returns 1 if inserted, 0 if updated."""
    metadata_json = json.dumps(metadata, ensure_ascii=False)

    # Check if exists
    existing = session.execute(
        text("SELECT id FROM entities WHERE entity_type = :et AND code = :c"),
        {"et": entity_type, "c": code}
    ).first()

    if existing:
        # Update
        session.execute(
            text("""
                UPDATE entities
                SET name_en = COALESCE(:ne, name_en),
                    name_zh = COALESCE(:nz, name_zh),
                    metadata_json = :meta,
                    updated_at = NOW()
                WHERE entity_type = :et AND code = :c
            """),
            {"et": entity_type, "c": code, "ne": name_en, "nz": name_zh, "meta": metadata_json}
        )
        return 0
    else:
        # Insert
        session.execute(
            text("""
                INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json)
                VALUES (:et, :c, :ne, :nz, :meta)
            """),
            {"et": entity_type, "c": code, "ne": name_en, "nz": name_zh, "meta": metadata_json}
        )
        return 1


def upsert_relationship(session: Session, source_id: int, target_id: int,
                        rel_type: str = "belongs_to") -> None:
    """Upsert a relationship between two entities."""
    # Check if already exists
    existing = session.execute(
        text("""
            SELECT id FROM entity_relationships
            WHERE source_id = :src AND target_id = :tgt
        """),
        {"src": source_id, "tgt": target_id}
    ).first()

    if not existing:
        session.execute(
            text("""
                INSERT INTO entity_relationships (source_id, target_id, relation_type)
                VALUES (:src, :tgt, :rel)
            """),
            {"src": source_id, "tgt": target_id, "rel": rel_type}
        )


def log_sync(session: Session, status: str, error_msg: Optional[str] = None,
             duration_seconds: int = 0, **counts) -> None:
    """Log sync run to entity_sync_logs table."""
    session.execute(
        text("""
            INSERT INTO entity_sync_logs
            (entity_type, started_at, finished_at, inserted_count, updated_count,
             error_count, status, error_message, duration_seconds)
            VALUES ('industry', NOW(), NOW(), :inserted, :updated, :errors,
                    :status, :err, :dur)
        """),
        {
            "inserted": counts.get("industries_inserted", 0) + counts.get("stocks_inserted", 0),
            "updated": counts.get("industries_updated", 0) + counts.get("stocks_updated", 0),
            "errors": len(counts.get("errors", [])),
            "status": status,
            "err": error_msg,
            "dur": duration_seconds,
        }
    )


def sync_sw_industries() -> dict:
    """Main sync function. Returns summary dict."""
    start_time = time.time()
    session = get_db_session()

    summary = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "industries_inserted": 0,
        "industries_updated": 0,
        "stocks_inserted": 0,
        "stocks_updated": 0,
        "relationships_created": 0,
        "errors": [],
    }

    try:
        # Step 1: Fetch all 31 申万 L1 industries
        print("Fetching 申万 L1 industries...")
        industries = fetch_sw_index_first_info()
        print(f"  Found {len(industries)} industries")

        # Step 2: For each industry, insert/upsert and fetch constituents
        industry_ids = {}
        for ind in industries:
            code = ind["code"]

            try:
                # Upsert industry entity
                inserted = upsert_entity(
                    session=session,
                    entity_type="industry",
                    code=code,
                    name_en=None,  # No English name in 申万
                    name_zh=ind["name_zh"],
                    metadata={"sw_l1": True, "classification_system": "shenwan"}
                )

                if inserted:
                    summary["industries_inserted"] += 1
                    print(f"  [NEW] {code}: {ind['name_zh']} ({ind['component_count']} stocks)")
                else:
                    summary["industries_updated"] += 1
                    print(f"  [UPD] {code}: {ind['name_zh']}")

                # Get entity ID for later
                result = session.execute(
                    text("SELECT id FROM entities WHERE entity_type = 'industry' AND code = :c"),
                    {"c": code}
                ).first()
                industry_ids[code] = result.id

                # Fetch and upsert constituents
                constituents = fetch_sw_constituents(code)
                for stock in constituents:
                    # Upsert stock entity first
                    stock_id_result = session.execute(
                        text("""
                            SELECT id FROM entities
                            WHERE entity_type = 'stock' AND code = :c
                        """),
                        {"c": stock["stock_code"]}
                    ).first()

                    if not stock_id_result:
                        inserted_stock = upsert_entity(
                            session=session,
                            entity_type="stock",
                            code=stock["stock_code"],
                            name_en=None,
                            name_zh=stock["stock_name"],
                            metadata={"exchange": "SSE/SZSE", "has_sw_industry": True}
                        )
                        if inserted_stock:
                            summary["stocks_inserted"] += 1

                        # Get newly created ID
                        stock_id_result = session.execute(
                            text("SELECT id FROM entities WHERE entity_type = 'stock' AND code = :c"),
                            {"c": stock["stock_code"]}
                        ).first()
                    else:
                        summary["stocks_updated"] += 1

                    stock_id = stock_id_result.id

                    # Link stock → industry
                    upsert_relationship(
                        session=session,
                        source_id=stock_id,
                        target_id=industry_ids[code],
                        rel_type="belongs_to"
                    )
                    summary["relationships_created"] += 1

                # Commit after each industry to prevent transaction abort cascade
                session.commit()

            except Exception as e:
                session.rollback()
                msg = f"Failed to process {code}: {e}"
                summary["errors"].append(msg)
                print(f"  ERROR {code}: {e}")
                continue

        session.commit()

    except Exception as e:
        session.rollback()
        summary["errors"].append(str(e))
        traceback.print_exc()
        raise

    finally:
        duration = int(time.time() - start_time)
        summary["end_time"] = datetime.now(timezone.utc).isoformat()
        summary["duration_seconds"] = duration
        summary["summary"] = f"{summary['industries_inserted']}+{summary['industries_updated']} industries, " \
                            f"{summary['stocks_inserted']}+{summary['stocks_updated']} stocks, " \
                            f"{summary['relationships_created']} relationships"

        log_sync(
            session=session,
            status="ok" if not summary["errors"] else "error",
            error_msg="; ".join(summary["errors"]) if summary["errors"] else None,
            duration_seconds=duration,
            **{k: v for k, v in summary.items() if k not in ["errors", "start_time", "end_time", "summary", "duration_seconds"]}
        )

        session.close()

    return summary


if __name__ == "__main__":
    print("=" * 60)
    print("Shenwan Industry Sync")
    print("=" * 60)

    try:
        summary = sync_sw_industries()

        print("\n" + "=" * 60)
        print("Sync Complete!")
        print("=" * 60)
        print(f"Summary: {summary['summary']}")
        print(f"Duration: {summary['duration_seconds']}s")

        if summary["errors"]:
            print(f"\nErrors ({len(summary['errors'])}):")
            for err in summary["errors"]:
                print(f"  - {err}")

    except Exception as e:
        print(f"\nSync FAILED: {e}")
        sys.exit(1)
