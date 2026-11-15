"""Alembic chain invariants — the alembic_version column can hold every id.

2026-11-15: ``0024_federated_runner_pin_c33bf67`` is 33 characters while
``alembic_version.version_num`` is alembic's default ``varchar(32)``, so
``UPDATE alembic_version SET version_num='0024_…'`` raised
``StringDataRightTruncation`` and **the whole upgrade rolled back**: an empty
database could never reach head, and the startup schema gate (``db/__init__.py``)
refuses to boot an image whose head the database has not reached. The visible
symptom was Build Check red on main ("Migrate empty database to head") and — had
it shipped — a panel that CrashLoops instead of serving the login station.

The fix lives in 0024 itself (it widens the column before stamping). These tests
pin the two things that made it invisible:

1. every revision id must fit the widened bound (so the next long name fails
   here with a readable message instead of inside a pod's init container);
2. the revision that first exceeds the old 32-char default must widen the column
   **before** the stamp — i.e. the ALTER has to be in its upgrade body.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = PROJECT_ROOT / "alembic" / "versions"

# The bound 0024 sets. Raising it means widening the column in a new revision
# first — the point of pinning it in a test is that you cannot do one without
# the other.
MAX_REVISION_LEN = 64
# alembic's own default when it creates the ledger.
ALEMBIC_DEFAULT_LEN = 32


def _revision_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py")


def _revision_meta(path: Path) -> dict:
    """(revision, down_revision, source) parsed from the module without importing
    it — importing would execute migration code."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    meta: dict = {"source": src, "path": path}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in ("revision", "down_revision") and isinstance(node.value, ast.Constant):
                meta[target.id] = node.value.value
    return meta


@pytest.fixture(scope="module")
def revisions() -> list[dict]:
    files = _revision_files()
    assert files, f"no revisions found under {VERSIONS_DIR}"
    return [_revision_meta(p) for p in files]


def test_every_revision_id_fits_the_version_column(revisions):
    too_long = [(m.get("revision"), len(m.get("revision") or "")) for m in revisions
                if len(m.get("revision") or "") > MAX_REVISION_LEN]
    assert not too_long, (
        f"revision id(s) longer than {MAX_REVISION_LEN} chars: {too_long}. "
        "alembic stamps these into alembic_version.version_num — widen the column "
        "in the revision that first exceeds the current bound, then raise "
        "MAX_REVISION_LEN here."
    )


def test_revision_ids_are_unique(revisions):
    ids = [m.get("revision") for m in revisions]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate revision id(s): {sorted(dupes)}"


def test_chain_is_linear_and_reaches_a_single_head(revisions):
    """One head, no forks, and every down_revision resolves (or is None).

    A fork silently makes `upgrade head` ambiguous; a dangling down_revision makes
    an empty-database migration fail at that step, which is the same class of
    outage as the 33-char id.
    """
    ids = {m.get("revision") for m in revisions}
    referenced = set()
    for m in revisions:
        down = m.get("down_revision")
        if down is not None:
            assert down in ids, (
                f"{m['path'].name}: down_revision {down!r} is not a revision in "
                f"{VERSIONS_DIR.name}/ — the chain is broken")
            referenced.add(down)
    heads = sorted(ids - referenced)
    assert len(heads) == 1, f"expected exactly one head, got {heads}"


def _exec_driver_sql_literals(func_node: ast.FunctionDef) -> list[str]:
    """SQL strings passed as the first argument to ``*.exec_driver_sql(...)``.

    Parsed from the AST rather than the text: Python folds adjacent literals
    (``"ALTER TABLE x " "ALTER COLUMN y"``) into one Constant at compile time, so
    the AST sees the joined statement while a regex over the source sees two
    quoted fragments and matches neither.
    """
    out: list[str] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None)
        if name != "exec_driver_sql" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
    return out


def test_the_revision_that_exceeds_the_default_widens_the_column(revisions):
    """The first id longer than alembic's varchar(32) must widen the column in its
    own upgrade body — alembic stamps only after the body returns."""
    over = [m for m in revisions
            if len(m.get("revision") or "") > ALEMBIC_DEFAULT_LEN]
    if not over:
        pytest.skip("no revision exceeds alembic's default 32 yet")
    first = min(over, key=lambda m: m["revision"])
    tree = ast.parse(first["source"])
    upgrade = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
    assert upgrade is not None, f"{first['path'].name}: no upgrade()"
    widened = [sql for sql in _exec_driver_sql_literals(upgrade)
               if re.search(r"ALTER TABLE\s+alembic_version\s+ALTER COLUMN\s+version_num"
                            r"\s+TYPE\s+varchar\(\d+\)", re.sub(r"\s+", " ", sql))]
    assert widened, (
        f"{first['path'].name}: id {first['revision']!r} is "
        f"{len(first['revision'])} chars, longer than alembic's default "
        f"varchar({ALEMBIC_DEFAULT_LEN}), but its upgrade() never widens "
        "alembic_version.version_num — the final stamp will fail with "
        "StringDataRightTruncation and roll the whole migration back")


# ── integration: an empty database must reach head ───────────────────────────
PG_HOST, PG_PORT, PG_USER = "127.0.0.1", 55432, "chengsishi"


def _pg_reachable() -> bool:
    import socket
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_reachable(),
                    reason=f"scratch postgres at {PG_HOST}:{PG_PORT} unreachable")
def test_empty_database_migrates_to_head():
    """The Build Check step, locally: fresh DB -> `alembic upgrade head` -> stamped.

    This is the criterion the 33-char id broke; asserting the *stamp* (not just
    exit code) is what makes it fail loudly, because alembic exits non-zero but a
    caller that only checks "tables exist" would call it a success.
    """
    import os
    import subprocess
    import sys
    import uuid

    import psycopg2
    from alembic.script import ScriptDirectory

    head = ScriptDirectory(str(PROJECT_ROOT / "alembic")).get_current_head()
    db = "alb_test_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/postgres")
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db}"')
        env = dict(os.environ)
        env["FD_OPEN_DATA_MCP_DATABASE_URL"] = (
            f"postgresql+psycopg2://{PG_USER}@{PG_HOST}:{PG_PORT}/{db}")
        proc = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                              cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                              env=env, timeout=180)
        assert proc.returncode == 0, (
            "alembic upgrade head failed on an empty database:\n"
            + (proc.stdout + proc.stderr)[-1500:])
        conn = psycopg2.connect(f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{db}")
        try:
            with conn.cursor() as cur:
                cur.execute("select version_num from alembic_version")
                stamp = cur.fetchone()[0]
                cur.execute("select character_maximum_length from information_schema.columns "
                            "where table_name='alembic_version' and column_name='version_num'")
                width = cur.fetchone()[0]
        finally:
            conn.close()
        assert stamp == head, f"stamped {stamp!r}, expected head {head!r}"
        assert width is None or width >= len(head), (
            f"version_num is varchar({width}) but the head id is {len(head)} chars")
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        admin.close()
