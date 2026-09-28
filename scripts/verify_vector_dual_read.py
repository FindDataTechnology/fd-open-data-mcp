"""Dual-read verification: JSON cosine path vs pgvector ``embedding_vec`` path.

mcp-search-engine-overhaul task 3.2. After revision 0003_pgvector_embedding_columns
backfills the new vector columns, the old Python brute-force path and the new
pgvector path must return the same neighbours. This script proves it against a
real database:

- samples N rows that carry both the JSON embedding and the vector column;
- path A (legacy): SELECT all JSON embeddings, cosine similarity in Python,
  top-k by (-sim, id);
- path B (pgvector): ``SELECT id, 1 - (embedding_vec <=> :qv::vector) AS sim
  ... ORDER BY embedding_vec <=> :qv::vector LIMIT :k``;
- compares the two top-k lists per sample (Jaccard over the id sets, Kendall
  tau over the shared ordering) and fails unless every sampled table averages
  Jaccard >= 0.95 and tau >= 0.9.

Read-only (SELECTs only — it never writes, DDL included). The database is
taken from FD_OPEN_DATA_MCP_DATABASE_URL; there is no default: verification
targets are always explicit. PostgreSQL only — the ``<=>`` operator does not
exist elsewhere.

Usage:
  FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://... \\
      python scripts/verify_vector_dual_read.py [--samples 50] [--topk 20] \\
          [--table concept|entity|both] [--seed 0]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from typing import Any, Sequence

from sqlalchemy import create_engine, text

TABLES = {"concept": "concept_embeddings", "entity": "entity_embeddings"}

JACCARD_THRESHOLD = 0.95
TAU_THRESHOLD = 0.9


# ---------------------------------------------------------------------------
# Pure comparison logic (unit-tested without a database)
# ---------------------------------------------------------------------------

def jaccard(a: Sequence[Any], b: Sequence[Any]) -> float:
    """|A∩B| / |A∪B|; two empty sets agree completely."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


def kendall_tau(rank_a: Sequence[Any], rank_b: Sequence[Any]) -> float:
    """Kendall tau over the items common to both rankings.

    Ids are unique, so there are no ties and tau-a == tau-b. Fewer than two
    shared items cannot contradict any ordering: defined as 1.0.
    """
    sb = set(rank_b)
    common = [x for x in rank_a if x in sb]
    n = len(common)
    if n < 2:
        return 1.0
    sa = set(common)
    pos_b = {item: i for i, item in enumerate(x for x in rank_b if x in sa)}
    seq = [pos_b[item] for item in common]
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if seq[i] < seq[j]:
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


def parse_embedding(raw: Any) -> list[float]:
    """One parser for every storage form the two tables use.

    JSONB arrives from psycopg2 as a Python list; the TEXT column arrives as a
    JSON string; both '[1.0, 2.0]' (JSON) and '1.0,2.0' (pgvector text form)
    are accepted so the same code reads query vectors back from either path.
    """
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if not (s.startswith("[") and s.endswith("]")):
            s = f"[{s}]"
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return [float(x) for x in s.strip("[]").split(",") if x.strip()]
        if isinstance(parsed, (int, float)):
            return [float(parsed)]
        return [float(x) for x in parsed]
    raise TypeError(f"unrecognised embedding payload: {type(raw)!r}")


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine of two vectors; zero vectors have no direction -> 0.0."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_python(
    ids: Sequence[Any],
    vectors: Sequence[Sequence[float]],
    query: Sequence[float],
    topk: int,
) -> list[Any]:
    """Path A: brute-force cosine over the JSON payloads, top-k ids.

    Ties break by id so the ranking is deterministic.
    """
    scored = [
        (cosine_similarity(query, vec), row_id)
        for row_id, vec in zip(ids, vectors)
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [row_id for _, row_id in scored[:topk]]


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector input literal for a bind parameter (also valid JSON text)."""
    return json.dumps([float(x) for x in vec])


def compare_topk(list_a: Sequence[Any], list_b: Sequence[Any]) -> dict[str, Any]:
    return {
        "jaccard": jaccard(list_a, list_b),
        "tau": kendall_tau(list_a, list_b),
        "only_json": [x for x in list_a if x not in set(list_b)],
        "only_vector": [x for x in list_b if x not in set(list_a)],
        "json_order": list(list_a),
        "vector_order": list(list_b),
    }


# ---------------------------------------------------------------------------
# Database verification
# ---------------------------------------------------------------------------

def _require_columns(engine, table: str) -> None:
    """Fail with a pointer to the migration when the vector column is absent."""
    with engine.connect() as conn:
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :t"
                ),
                {"t": table},
            )
        }
    missing = {"id", "embedding", "embedding_vec"} - columns
    if missing:
        raise SystemExit(
            f"{table}: missing column(s) {sorted(missing)} — run "
            "`alembic upgrade head` (revision 0003_pgvector_embedding_columns) "
            "before verifying dual reads"
        )


def verify_table(engine, table: str, samples: int, topk: int, seed: int = 0,
                 ef_search: int = 200) -> dict[str, Any]:
    """Dual-read one table; returns a metrics dict (never raises on mismatch)."""
    _require_columns(engine, table)

    with engine.connect() as conn:
        # Path A needs the whole corpus anyway; only rows both paths can rank
        # are considered, so the two candidate sets are identical.
        rows = conn.execute(
            text(
                f"SELECT id, embedding FROM {table} "
                "WHERE embedding IS NOT NULL AND embedding_vec IS NOT NULL"
            )
        ).fetchall()

    if not rows:
        return {"table": table, "samples": 0, "skipped": True}

    ids = [r[0] for r in rows]
    vectors = [parse_embedding(r[1]) for r in rows]

    rng = random.Random(seed)
    n = min(samples, len(ids))
    picked = rng.sample(range(len(ids)), n)

    comparisons = []
    with engine.connect() as conn:
        # Wider HNSW beam for recall parity with the exact JSON ranking
        # (default ef_search=40 misses near-duplicate-cluster neighbors).
        conn.execute(text(f"SET hnsw.ef_search = {ef_search}"))
        conn.execute(text("SET hnsw.iterative_scan = 'relaxed_order'"))
        for i in picked:
            query = vectors[i]
            # Path B: pgvector cosine distance ordering, same top-k. The
            # similarity projection matches the production query shape.
            ranked = conn.execute(
                text(
                    f"SELECT id, 1 - (embedding_vec <=> CAST(:qv AS vector)) AS sim "
                    f"FROM {table} "
                    "ORDER BY embedding_vec <=> CAST(:qv AS vector) "
                    "LIMIT :k"
                ),
                {"qv": vector_literal(query), "k": topk},
            ).fetchall()
            vector_ids = [r[0] for r in ranked]
            json_ids = rank_python(ids, vectors, query, topk)
            comparisons.append((ids[i], compare_topk(json_ids, vector_ids)))

    jaccards = [c["jaccard"] for _, c in comparisons]
    taus = [c["tau"] for _, c in comparisons]
    return {
        "table": table,
        "samples": len(comparisons),
        "skipped": False,
        "mean_jaccard": sum(jaccards) / len(jaccards),
        "mean_tau": sum(taus) / len(taus),
        "passed": (
            sum(jaccards) / len(jaccards) >= JACCARD_THRESHOLD
            and sum(taus) / len(taus) >= TAU_THRESHOLD
        ),
        "mismatches": [
            {"sample_id": sample_id, **c}
            for sample_id, c in comparisons
            if c["jaccard"] < 1.0 or c["tau"] < 1.0
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify JSON cosine and pgvector search paths agree."
    )
    parser.add_argument("--samples", type=int, default=50,
                        help="random query rows per table (default 50)")
    parser.add_argument("--topk", type=int, default=20,
                        help="top-k neighbours compared per sample (default 20)")
    parser.add_argument("--table", choices=("concept", "entity", "both"),
                        default="both", help="which table to verify (default both)")
    parser.add_argument("--ef-search", type=int, default=200,
                        help="hnsw.ef_search for the vector path (recall beam)")
    parser.add_argument("--seed", type=int, default=0,
                        help="sampling seed for reproducible runs (default 0)")
    args = parser.parse_args(argv)

    url = os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not url:
        raise SystemExit(
            "Set FD_OPEN_DATA_MCP_DATABASE_URL to the target database before "
            "running this verifier (no default — verification targets are "
            "explicit)."
        )
    if url.startswith("sqlite"):
        raise SystemExit(
            "The pgvector path (<=> operator, HNSW) only exists on PostgreSQL; "
            "point FD_OPEN_DATA_MCP_DATABASE_URL at the migrated PG database."
        )

    tables = list(TABLES) if args.table == "both" else [args.table]
    engine = create_engine(url)
    try:
        results = [verify_table(engine, TABLES[t], args.samples, args.topk, args.seed,
                              ef_search=args.ef_search)
                   for t in tables]
    finally:
        engine.dispose()

    verified = [r for r in results if not r["skipped"]]
    for r in results:
        if r["skipped"]:
            print(f"[{r['table']}] WARNING: no rows carry both embedding and "
                  "embedding_vec — nothing to verify (skipped)")
            continue
        status = "PASS" if r["passed"] else "FAIL"
        print(
            f"[{r['table']}] {status}: samples={r['samples']} "
            f"mean_top{args.topk}_jaccard={r['mean_jaccard']:.4f} "
            f"(>= {JACCARD_THRESHOLD}) mean_kendall_tau={r['mean_tau']:.4f} "
            f"(>= {TAU_THRESHOLD})"
        )
        for m in r["mismatches"]:
            print(
                f"  sample id={m['sample_id']} jaccard={m['jaccard']:.4f} "
                f"tau={m['tau']:.4f}\n"
                f"    only_json={m['only_json']} only_vector={m['only_vector']}\n"
                f"    json_order={m['json_order']}\n"
                f"    vector_order={m['vector_order']}"
            )

    if not verified:
        print("DUAL-READ VERIFICATION INCONCLUSIVE: no eligible rows in any table")
        return 1
    if all(r["passed"] for r in verified):
        print(f"DUAL-READ VERIFICATION PASS ({', '.join(r['table'] for r in verified)})")
        return 0
    print("DUAL-READ VERIFICATION FAIL: see mismatching samples above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
