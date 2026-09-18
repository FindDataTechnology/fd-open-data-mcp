"""Check that vocabulary-bearing database content traces to fd-open-data-protocol (design D8).

The protocol package is the source of truth for naming: every non-empty
``concepts.concept_code`` must resolve to a concept family id in the
protocol's vocabulary (``load_vocabulary().ids("concept")``). A row that
does not is drift — e.g. a hand-written or stale family code that no longer
exists upstream — and this check reports it.

Importable core (used by tests and CI):

    from check_vocabulary_conformance import conformance_problems
    problems = conformance_problems(engine)   # [] == conformant

CLI usage:

    FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://... \\
        python scripts/check_vocabulary_conformance.py

Exit codes: 0 = conformant (or nothing to check — see below), 1 = violations
found, 2 = misuse (no database URL configured).

A database without a ``concepts`` table (pre-adoption, migrations never run)
is not a conformance violation: the CLI prints a notice and exits 0.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from fd_open_data_protocol.vocabulary import load_vocabulary

# How many offending values/rows to spell out before collapsing to a count.
SAMPLE_LIMIT = 20

CONCEPTS_TABLE = "concepts"


def conformance_problems(engine: Engine, *, sample_limit: int = SAMPLE_LIMIT) -> list[str]:
    """Return a list of human-readable conformance problems (empty == conformant).

    Every non-empty ``concepts.concept_code`` that does not resolve to a
    protocol concept family id yields one problem string carrying the
    vocabulary version, the offending value, its row count and sample
    concept codes. At most ``sample_limit`` offending values are reported;
    the remainder are summarised in a final count line. Dialect-agnostic:
    works on PostgreSQL and SQLite.

    A missing ``concepts`` table (pre-adoption database) yields no problems —
    there is no vocabulary-bearing content to check. Callers that need to
    distinguish that case can use :func:`concepts_table_exists`.
    """
    if not concepts_table_exists(engine):
        return []

    vocabulary = load_vocabulary()
    family_ids = set(vocabulary.ids("concept"))
    version = getattr(vocabulary, "version", "unknown")

    offenders: Counter[str] = Counter()          # concept_code -> row count
    samples: dict[str, list[str]] = {}           # concept_code -> up to 3 concept codes
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, code, concept_code FROM concepts "
            "WHERE concept_code IS NOT NULL AND concept_code <> ''"
        ))
        for row in rows:
            code = row.concept_code
            if code not in family_ids:
                offenders[code] += 1
                samples.setdefault(code, [])
                if len(samples[code]) < 3:
                    samples[code].append(f"{row.code}#id={row.id}")

    problems: list[str] = []
    for bad_code in sorted(offenders):
        examples = ", ".join(samples.get(bad_code, []))
        problems.append(
            f"concepts.concept_code '{bad_code}' resolves to no concept family "
            f"in fd-open-data-protocol vocabulary version {version} "
            f"({offenders[bad_code]} row(s); e.g. {examples})"
        )
    if len(problems) > sample_limit:
        extra = len(problems) - sample_limit
        problems = problems[:sample_limit]
        problems.append(
            f"...and {extra} more offending concept_code value(s) against "
            f"vocabulary version {version}"
        )
    return problems


def concepts_table_exists(engine: Engine) -> bool:
    """True if the ``concepts`` table is present (False on pre-adoption DBs)."""
    return inspect(engine).has_table(CONCEPTS_TABLE)


def main(argv: list[str] | None = None) -> int:
    url = os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not url:
        print("FD_OPEN_DATA_MCP_DATABASE_URL is not set — nothing to check.", file=sys.stderr)
        return 2

    engine = create_engine(url)
    try:
        if not concepts_table_exists(engine):
            print(
                f"note: table '{CONCEPTS_TABLE}' not found (pre-adoption database?) "
                "— no vocabulary-bearing content to check"
            )
            return 0

        problems = conformance_problems(engine)
        version = getattr(load_vocabulary(), "version", "unknown")
        if not problems:
            print(f"OK: concepts.concept_code traces to fd-open-data-protocol vocabulary {version}")
            return 0

        print(
            f"FAIL: {len(problems)} problem(s) against fd-open-data-protocol "
            f"vocabulary version {version}:"
        )
        for problem in problems[:SAMPLE_LIMIT]:
            print(f"  - {problem}")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
