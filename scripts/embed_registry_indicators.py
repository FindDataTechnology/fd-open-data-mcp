"""Batch-embed the unified indicator registry (mcp-search-engine-overhaul 4.1, design D5).

Embeds every ``registry_entries`` row that has no embedding yet, using the
shared sentence-transformer (``fd_open_data_mcp.embeddings.model`` — resolved
by name, never a machine-specific path). Corpus text per row:
``"name_zh | name_en | semantic_code"`` (empty segments skipped; all-empty
rows are counted and skipped).

Resume-by-rerun: each batch is committed immediately and the pending set is
re-derived with a LEFT JOIN, so an interrupted run needs no journal — rerun
the script and it continues with the rows that are still unembedded.

Usage:
    FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://... \
        python scripts/embed_registry_indicators.py \
        [--batch-size 256] [--limit N] [--dry-run]

``--dry-run`` counts only (no model load, no writes).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.semantic import registry_embeddings


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Batch-embed unified registry indicators for semantic search.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=256,
        help="Rows fetched, encoded and committed per batch (default 256).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum rows to embed this run (default: all pending).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count pending rows only — no model load, no writes.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"):
        raise SystemExit(
            "Set FD_OPEN_DATA_MCP_DATABASE_URL to the target database before "
            "running this job (no default — targets are explicit)."
        )

    started = time.time()
    db = dbmod.get_database()
    session = db.get_session()
    try:
        if not registry_embeddings.tables_present(session):
            raise SystemExit(
                "registry_entries / registry_indicator_embeddings not present "
                "on the target database — run the registry DDL and the "
                "embeddings migration first."
            )

        pending = registry_embeddings.count_unembedded_entries(session)
        unembeddable = registry_embeddings.count_unembeddable_entries(session)
        already = registry_embeddings.count_embeddings(session)
        print(f"Registry embeddings: {already} stored, {pending} pending, "
              f"{unembeddable} not embeddable (all text fields empty).")

        if args.dry_run:
            print("Dry run: no rows embedded (no model loaded, nothing written).")
            return 0

        model = registry_embeddings.get_model()
        with_vector = registry_embeddings.vector_column_exists(session)
        print(f"Model: {registry_embeddings.MODEL_NAME} "
              f"(dim {model.get_embedding_dimension()}); "
              f"vector column {'on' if with_vector else 'off (JSON only)'}.")

        budget = args.limit if args.limit is not None else float("inf")
        embedded_run = 0
        skipped_empty = 0
        last_id = 0
        while embedded_run < budget:
            rows = registry_embeddings.fetch_unembedded_entries(
                session, model=registry_embeddings.MODEL_NAME,
                after_id=last_id, batch_size=args.batch_size,
            )
            if not rows:
                break

            work = []
            for row in rows:
                entry_text = registry_embeddings.build_embed_text(
                    row["name_zh"], row["name_en"], row["semantic_code"],
                )
                if entry_text is None:
                    skipped_empty += 1
                    continue
                work.append((row["id"], entry_text))
            last_id = rows[-1]["id"]

            if work:
                vectors = model.encode(
                    [entry_text for _, entry_text in work],
                    batch_size=args.batch_size,
                )
                for (entry_id, entry_text), vector in zip(work, vectors):
                    registry_embeddings.upsert_embedding(
                        session, entry_id, vector, entry_text,
                        model=registry_embeddings.MODEL_NAME,
                        with_vector_column=with_vector,
                    )
                session.commit()  # per-batch commit -> rerun resumes here
                embedded_run += len(work)
                remaining = registry_embeddings.count_unembedded_entries(session)
                print(f"  batch done: +{len(work)} (run total {embedded_run}), "
                      f"{remaining} pending")

        elapsed = time.time() - started
        total = registry_embeddings.count_embeddings(session)
        still_pending = registry_embeddings.count_unembedded_entries(session)
        print(
            f"\nDone in {elapsed:.1f}s: embedded {embedded_run} rows this run "
            f"(skipped {skipped_empty} all-empty), total embedded {total} "
            f"for model '{registry_embeddings.MODEL_NAME}', "
            f"{still_pending} still pending."
        )
        if still_pending:
            print("Rerun the script to continue from where it stopped.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
