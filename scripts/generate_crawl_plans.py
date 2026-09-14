#!/usr/bin/env python3
"""Generate backfill + incremental CrawlPlan pair for schedule-activation trial.

Creates:
1. Backfill plan: full date range (e.g., last 2 years for daily, 5 years for quarterly)
2. Incremental plan: since_last=True (watermark-based)

Both plans use the bound concepts and entity identifiers for stock entity_type.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.crawl.planner import plan_crawl, EntityScope, DateRange
from fd_open_data_mcp.models import Concept, EntitySourceIdentifier

DEFAULT_URL = "sqlite:////tmp/schedule_activation_trial.db"
OUTPUT_DIR = Path("/Users/chengsishi/finddata/scraw-fd-open-data-mcp/output/schedule-activation")

# Entity IDs for trial (blue-chip A-shares)
TRIAL_ENTITY_IDS = [600000, 600036, 600519, 600887, 601318]  # 浦发银行, 招商银行, 贵州茅台, 伊利股份, 中国平安

# Cold-start window per cadence: >=2 periods of the concept's own frequency, so a
# bootstrap incremental run covers the most recently published row of every cadence.
INCREMENTAL_LOOKBACK_DAYS = {
    "daily": 14, "weekly": 42, "monthly": 120, "quarterly": 300, "yearly": 800,
}

def add_trial_entities(session: Session) -> None:
    """Add trial entity identifiers for akshare source."""
    src = "akshare"
    for eid in TRIAL_ENTITY_IDS:
        ident = session.query(EntitySourceIdentifier).filter_by(
            entity_type="stock", entity_id=eid, source=src
        ).first()
        if ident is None:
            ident = EntitySourceIdentifier(
                entity_type="stock",
                entity_id=eid,
                source=src,
                identifier=str(eid),  # bare code for akshare
            )
            session.add(ident)
    session.commit()
    print(f"Added {len(TRIAL_ENTITY_IDS)} entity identifiers for source '{src}'")


def get_concept_ids(session: Session, entity_type: str = "stock") -> list[int]:
    """Get all concept IDs for a given entity_type that have bindings."""
    concepts = session.query(Concept).filter(
        Concept.entity_type == entity_type,
        Concept.deprecated == False,
    ).all()
    # Filter to those with at least one binding
    valid = []
    for c in concepts:
        if c.bindings:
            valid.append(c.id)
    return valid


def generate_plans(db_url: str, output_dir: Path, dry_run: bool = False) -> tuple[Path, Path]:
    """Generate backfill and incremental crawl plans."""
    eng = create_engine(db_url, echo=False)
    SF = sessionmaker(bind=eng)

    with SF() as session:
        # Add trial entities
        add_trial_entities(session)

        # Get stock concept IDs with bindings
        concept_ids = get_concept_ids(session, "stock")
        print(f"Found {len(concept_ids)} stock concepts with bindings")

        if not concept_ids:
            print("No concepts with bindings found!")
            return None, None

        # Define date ranges
        # Backfill: 2 years of daily data + 5 years of quarterly
        end_date = date.today().replace(day=1) - timedelta(days=1)  # end of last month
        start_daily = end_date.replace(year=end_date.year - 2)
        start_quarterly = end_date.replace(year=end_date.year - 5)

        # Backfill plan (full range, per_date mode for daily concepts)
        backfill_plan = plan_crawl(
            session=session,
            concept_ids=concept_ids,
            entity_scope=EntityScope(
                entity_type="stock",
                entity_ids=TRIAL_ENTITY_IDS,
            ),
            date_range=DateRange(
                start=start_daily.isoformat(),
                end=end_date.isoformat(),
                frequency="daily",  # planner handles per-concept frequency
            ),
            since_last=False,
            source_filter=["akshare"],
            mode="per_date",
        )

        # Incremental plan: watermark-driven (since_last) in steady state. At cold
        # start there is no watermark to advance from, and the planner refuses
        # ("no prior observations for --since-last, need explicit --start"), so the
        # call falls back to an explicit start derived from the widest cadence among
        # the routed concepts. An explicit start disables watermark derivation per
        # the planner contract, so this bootstrap window applies to the first run
        # only -- later runs advance from per-concept watermarks.
        entity_scope = EntityScope(entity_type="stock", entity_ids=TRIAL_ENTITY_IDS)

        incremental_plan = plan_crawl(
            session=session,
            concept_ids=concept_ids,
            entity_scope=entity_scope,
            date_range=DateRange(
                start=None,  # derived from watermark
                end=end_date.isoformat(),
                frequency="daily",
            ),
            since_last=True,
            source_filter=["akshare"],
            mode="per_date",
        )

        cold_start = not incremental_plan.wanted_concepts
        if cold_start:
            freqs = {
                c.frequency for c in
                session.query(Concept).filter(Concept.id.in_(concept_ids)).all()
            }
            lookback = max(INCREMENTAL_LOOKBACK_DAYS.get(f, 14) for f in freqs)
            incremental_plan = plan_crawl(
                session=session,
                concept_ids=concept_ids,
                entity_scope=entity_scope,
                date_range=DateRange(
                    start=(end_date - timedelta(days=lookback)).isoformat(),
                    end=end_date.isoformat(),
                    frequency="daily",
                ),
                since_last=False,
                source_filter=["akshare"],
                mode="per_date",
            )

    # Write plans
    output_dir.mkdir(parents=True, exist_ok=True)
    backfill_path = output_dir / "backfill_plan.json"
    incremental_path = output_dir / "incremental_plan.json"

    if not dry_run:
        backfill_path.write_text(backfill_plan.model_dump_json(indent=2))
        incremental_path.write_text(incremental_plan.model_dump_json(indent=2))
        print(f"Backfill plan written to {backfill_path} ({backfill_plan.plan_cells} cells)")
        print(f"Incremental plan written to {incremental_path} ({incremental_plan.plan_cells} cells)")

        # Print summary
        print("\n=== Backfill Plan Summary ===")
        print(f"  Wanted concepts: {len(backfill_plan.wanted_concepts)}")
        print(f"  Unroutable: {len(backfill_plan.unroutable)}")
        for u in backfill_plan.unroutable:
            print(f"    - {u}")
        print(f"  Unmapped: {len(backfill_plan.unmapped)}")
        print(f"  Plan cells: {backfill_plan.plan_cells}")

        print("\n=== Incremental Plan Summary ===")
        print(f"  Mode: {'cold-start bootstrap window' if cold_start else 'watermark (since_last)'}")
        print(f"  Window: {incremental_plan.date_range.start} -> {incremental_plan.date_range.end}")
        print(f"  Wanted concepts: {len(incremental_plan.wanted_concepts)}")
        print(f"  Unroutable: {len(incremental_plan.unroutable)}")
        for u in incremental_plan.unroutable:
            print(f"    - {u}")
        print(f"  Unmapped: {len(incremental_plan.unmapped)}")
        print(f"  Plan cells: {incremental_plan.plan_cells}")

    return backfill_path, incremental_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    generate_plans(args.db_url, args.output_dir, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())