#!/usr/bin/env python3
"""Run supervised SQLite trial for schedule-activation.

Executes a bounded trial crawl (one source, ≤3 entities, ≤30 days) against
the concept_crawl spider targeting the explicit SQLite database.

Requirements:
- Uses scraw-fd-open-data-mcp venv (has akshare + editable fd-open-data-mcp)
- Targets trial SQLite at /tmp/schedule_activation_trial.db
- Uses FD_PROXY_POOL=off for local egress
- Validates observations land in semantic_observations
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

DEFAULT_DB_URL = "sqlite:////tmp/schedule_activation_trial.db"
TRIAL_ENTITIES = [600000, 600036, 600519]  # 3 blue-chip stocks
SCRAW_DIR = "/Users/chengsishi/finddata/scraw-fd-open-data-mcp"


def create_trial_plan(db_url: str, output_path: Path) -> dict:
    """Create a bounded trial plan (3 entities, 30 days)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from fd_open_data_mcp.crawl.planner import plan_crawl, EntityScope, DateRange
    from fd_open_data_mcp.models import Concept

    eng = create_engine(db_url, echo=False)
    SF = sessionmaker(bind=eng)

    with SF() as session:
        # Get stock concept IDs with bindings
        concepts = session.query(Concept).filter(
            Concept.entity_type == "stock",
            Concept.deprecated == False,
        ).all()
        concept_ids = [c.id for c in concepts if c.bindings]

        # 30 days ending yesterday
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(days=29)

        plan = plan_crawl(
            session=session,
            concept_ids=concept_ids,
            entity_scope=EntityScope(
                entity_type="stock",
                entity_ids=TRIAL_ENTITIES,
            ),
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                frequency="daily",
            ),
            since_last=False,
            source_filter=["akshare"],
            mode="per_date",
        )

    # Write trial plan
    plan_dict = plan.model_dump()
    output_path.write_text(json.dumps(plan_dict, indent=2))
    # Read the count off the dumped dict, not the attribute: the scraw venv
    # (which runs the crawl) may ship an older released fd-open-data-mcp that
    # predates plan_cells.
    print(f"Trial plan: {plan_dict.get('plan_cells', '?')} cells "
          f"({len(TRIAL_ENTITIES)} entities × 30 days × {len(concept_ids)} concepts)")
    return plan_dict


def run_crawl(plan_path: Path, db_url: str) -> int:
    """Run concept_crawl spider against the trial SQLite.

    The project settings target the shared scrapy-redis queue (production fleet
    scheduling). A local single-run trial has no Redis and no queue to share, so
    the scheduler and dupefilter are overridden on the command line instead of
    edited out of settings.py — the cluster path stays untouched. SCRAW_CLUSTER_ID
    is deliberately unset: it only drives fleet egress registration, which needs
    the remote Postgres.
    """
    env = os.environ.copy()
    env["FD_OPEN_DATA_MCP_DATABASE_URL"] = db_url
    env["FD_PROXY_POOL"] = "off"  # Use local Mac egress
    env.pop("SCRAW_CLUSTER_ID", None)
    # akshare calls eastmoney directly; an inherited proxy var breaks it.
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(var, None)
    # The scraw venv ships a stale released fd-open-data-mcp (0.4.7) whose
    # instrumented_fetch() predates the function_id kwarg this tree passes, so
    # the executor cannot run against it. Put the source tree ahead on the path
    # rather than mutating the venv.
    src = "/Users/chengsishi/finddata/fd-open-data-mcp"
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src + (os.pathsep + prev if prev else "")

    # Run from scraw-fd-open-data-mcp directory with its venv
    cmd = [
        sys.executable, "-m", "scrapy", "crawl", "concept_crawl",
        "-a", f"plan={plan_path}",
        "-s", "SCHEDULER=scrapy.core.scheduler.Scheduler",
        "-s", "DUPEFILTER_CLASS=scrapy.dupefilters.RFPDupeFilter",
        "-s", "SCHEDULER_PERSIST=False",
        # The 503s are deterministic: eastmoney is unreachable from this Mac and
        # non-trading days carry no data, so retrying them cannot change the
        # outcome — it only inflates fetch_log. Off for a bounded trial.
        "-s", "RETRY_ENABLED=False",
        "-s", "LOG_LEVEL=INFO",
    ]

    print(f"Running: {' '.join(cmd)}")
    print(f"CWD: {SCRAW_DIR}")
    print(f"DB: {db_url}")

    result = subprocess.run(cmd, cwd=SCRAW_DIR, env=env, capture_output=False)
    return result.returncode


def verify_observations(db_url: str) -> dict:
    """Verify observations landed in semantic_observations."""
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy import text

    from fd_open_data_mcp.models import SemanticObservation, Concept

    eng = create_engine(db_url, echo=False)
    SF = sessionmaker(bind=eng)

    with SF() as session:
        # Count observations
        total = session.query(func.count(SemanticObservation.id)).scalar() or 0

        # By concept
        by_concept = session.execute(text("""
            SELECT c.code, c.entity_type, COUNT(*) as cnt
            FROM semantic_observations o
            JOIN concepts c ON c.id = o.concept_id
            GROUP BY c.id, c.code, c.entity_type
            ORDER BY cnt DESC
        """)).fetchall()

        # By entity
        by_entity = session.execute(text("""
            SELECT entity_type, entity_id, COUNT(*) as cnt
            FROM semantic_observations
            GROUP BY entity_type, entity_id
            ORDER BY cnt DESC
        """)).fetchall()

        # By date
        by_date = session.execute(text("""
            SELECT date, COUNT(*) as cnt
            FROM semantic_observations
            GROUP BY date
            ORDER BY date
        """)).fetchall()

        return {
            "total": total,
            "by_concept": [{"concept": r[0], "entity_type": r[1], "count": r[2]} for r in by_concept],
            "by_entity": [{"entity_type": r[0], "entity_id": r[1], "count": r[2]} for r in by_entity],
            "by_date": [{"date": r[0], "count": r[1]} for r in by_date],
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default=None,
                    help="explicit target; the supervised trial is SQLite-only "
                         "(design D3). Defaults to the local trial SQLite — never "
                         "the env-provided production Postgres.")
    ap.add_argument("--plan-output", type=Path, default=Path(
        "/Users/chengsishi/finddata/scraw-fd-open-data-mcp/output/schedule-activation/trial_plan.json"))
    ap.add_argument("--skip-plan", action="store_true", help="Use existing plan file")
    ap.add_argument("--skip-crawl", action="store_true", help="Only verify observations")
    args = ap.parse_args()

    # Never silently land on the env-provided production Postgres: it is
    # unreachable from this host, and a Postgres migration is an explicit
    # non-goal. Print the effective target and refuse anything but SQLite.
    db_url = args.db_url or DEFAULT_DB_URL
    if not db_url.startswith("sqlite"):
        print(f"Refusing target {db_url!r}: the supervised trial writes to an explicit "
              f"SQLite file (design D3). Pass --db-url sqlite:////path/to/trial.db.")
        return 2
    print(f"Effective DB URL: {db_url}"
          + ("" if args.db_url else "  (local trial default; --db-url not given)"))

    if not args.skip_plan:
        create_trial_plan(db_url, args.plan_output)

    if not args.skip_crawl:
        print("\n=== Running concept_crawl ===")
        rc = run_crawl(args.plan_output, db_url)
        if rc != 0:
            print(f"Crawl failed with exit code {rc}")
            return rc
        print("Crawl completed successfully")

    print("\n=== Verifying observations ===")
    stats = verify_observations(db_url)
    print(f"Total observations: {stats['total']}")
    print(f"\nBy concept:")
    for row in stats["by_concept"]:
        print(f"  {row['entity_type']}:{row['concept']:30s} = {row['count']}")
    print(f"\nBy entity:")
    for row in stats["by_entity"]:
        print(f"  {row['entity_type']}:{row['entity_id']} = {row['count']}")
    print(f"\nBy date:")
    for row in stats["by_date"][:10]:
        print(f"  {row['date']} = {row['count']}")
    if len(stats["by_date"]) > 10:
        print(f"  ... and {len(stats['by_date']) - 10} more dates")

    if stats["total"] == 0:
        print("\n⚠️  No observations recorded - trial may have issues")
        return 1

    print("\n✅ Trial successful - observations recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())