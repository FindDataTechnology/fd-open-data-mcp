"""Public face selection (openspec: public-read-surface).

Four honesty rules, each with the failure it prevents:

1. Declared-but-not-registered fails at startup (a shrunken face nobody notices
   is the `-32004 cold start` shape: the copilot answers nothing and no one can
   tell why).
2. An empty declaration means zero tools, never "no filtering = everything".
3. The `full` face removes nothing (unconfigured deployments unchanged).
4. The surface is read from the live provider, not a hand-maintained list — the
   static `@mcp.tool` grep in server.py finds 50 while the registration holds 87.
"""

from __future__ import annotations

import json

import pytest

from fd_open_data_mcp import public_face
from fd_open_data_mcp.public_face import (
    FaceConfigError,
    apply_face,
    declared_tool_names,
    load_public_declaration,
    registered_tool_names,
    resolve_face,
)


@pytest.fixture()
def mcp():
    """A fresh server instance, unmodified by any other test's face work."""
    import importlib

    from fd_open_data_mcp import server as server_module

    importlib.reload(server_module)
    return server_module.mcp


def test_registration_is_larger_than_the_decorator_scan():
    """Guards the reason the inventory is read from the provider, not grepped."""
    import importlib

    from fd_open_data_mcp import server as server_module

    importlib.reload(server_module)
    names = registered_tool_names(server_module.mcp)
    # The decorator families in server.py alone are ~50; the provider holds the
    # module-registered families too (policy_*, platform_*, restaurant_*, ...).
    assert len(names) > 60, f"provider reported only {len(names)} tools — inventory source is wrong"


def test_full_face_removes_nothing(mcp):
    before = registered_tool_names(mcp)
    kept = apply_face(mcp, "full")
    assert kept == before
    assert registered_tool_names(mcp) == before


def test_public_face_registers_exactly_the_declaration(mcp):
    declared = declared_tool_names(load_public_declaration())
    kept = apply_face(mcp, "public")
    assert kept == sorted(declared)
    assert registered_tool_names(mcp) == sorted(declared)


def test_public_face_keeps_no_write_or_ops_tools(mcp):
    """The curation rule itself: nothing that writes, crawls, or reads ops."""
    kept = set(apply_face(mcp, "public"))
    forbidden_prefixes = (
        "policy_", "platform_", "scope_", "auth_",
        "add_", "update_", "register_", "import_", "record_", "seed_",
        "confirm_", "review_", "propose_", "run_", "plan_", "generate_",
    )
    leaked = sorted(n for n in kept if n.startswith(forbidden_prefixes))
    assert not leaked, f"write/ops tools leaked into the public face: {leaked}"
    for exact in ("fetch", "rank_sources", "crawl_status", "coverage_report", "ai_search"):
        assert exact not in kept, f"{exact} must never reach visitors"


def test_empty_declaration_means_zero_tools(mcp):
    empty = {"schema": "mcp-tool-surface/v1", "tools": []}
    kept = apply_face(mcp, "public", declaration=empty)
    assert kept == []
    assert registered_tool_names(mcp) == [], "an empty declaration must not fall back to 'everything'"


def test_ghost_declaration_fails_loudly(mcp):
    ghost = {
        "schema": "mcp-tool-surface/v1",
        "tools": [{"name": "list_concepts", "why": "real"}, {"name": "no_such_tool", "why": "ghost"}],
    }
    with pytest.raises(FaceConfigError) as exc:
        apply_face(mcp, "public", declaration=ghost)
    assert "no_such_tool" in str(exc.value)


def test_declaration_entry_needs_a_reason(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"schema": "mcp-tool-surface/v1", "tools": [{"name": "read"}]}))
    with pytest.raises(FaceConfigError) as exc:
        load_public_declaration(path)
    assert "why" in str(exc.value)


def test_declaration_rejects_duplicates(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(
        json.dumps(
            {
                "schema": "mcp-tool-surface/v1",
                "tools": [
                    {"name": "read", "why": "a"},
                    {"name": "read", "why": "b"},
                ],
            }
        )
    )
    with pytest.raises(FaceConfigError) as exc:
        load_public_declaration(path)
    assert "twice" in str(exc.value)


def test_unknown_face_refuses_startup():
    with pytest.raises(FaceConfigError) as exc:
        resolve_face("bogus")
    msg = str(exc.value)
    assert "bogus" in msg and "full" in msg and "public" in msg


def test_unset_face_is_full(monkeypatch):
    monkeypatch.delenv(public_face.FACE_ENV, raising=False)
    assert resolve_face() == "full"
    monkeypatch.setenv(public_face.FACE_ENV, "  ")
    assert resolve_face() == "full"


# ── Data-range narrowing (spec: 只读不等于可公开) ────────────────────────────

class _Row:
    def __init__(self, verified, deprecated):
        self.verified = verified
        self.deprecated = deprecated


class _Session:
    """Minimal stand-in for the ORM session: get(Concept, id) by id."""

    def __init__(self, rows):
        self._rows = rows

    def get(self, _model, key):
        return self._rows.get(key)


def test_public_face_refuses_unverified_concept():
    from fd_open_data_mcp import public_face as pf

    pf.set_active_face("public")
    try:
        session = _Session({1: _Row(verified=False, deprecated=False)})
        with pytest.raises(PermissionError) as exc:
            pf.assert_concept_visible(session, 1)
        assert "verified" in str(exc.value)
    finally:
        pf.set_active_face("full")


def test_public_face_refuses_deprecated_concept():
    from fd_open_data_mcp import public_face as pf

    pf.set_active_face("public")
    try:
        session = _Session({2: _Row(verified=True, deprecated=True)})
        with pytest.raises(PermissionError):
            pf.assert_concept_visible(session, 2)
    finally:
        pf.set_active_face("full")


def test_public_face_refuses_unknown_id():
    from fd_open_data_mcp import public_face as pf

    pf.set_active_face("public")
    try:
        with pytest.raises(PermissionError) as exc:
            pf.assert_concept_visible(_Session({}), 999)
        assert "unknown id" in str(exc.value)
    finally:
        pf.set_active_face("full")


def test_public_face_admits_verified_concept():
    from fd_open_data_mcp import public_face as pf

    pf.set_active_face("public")
    try:
        pf.assert_concept_visible(_Session({3: _Row(verified=True, deprecated=False)}), 3)
    finally:
        pf.set_active_face("full")


def test_full_face_does_not_narrow_data():
    """The guard is inert off the public face — existing behavior byte-for-byte."""
    from fd_open_data_mcp import public_face as pf

    pf.set_active_face("full")
    session = _Session({1: _Row(verified=False, deprecated=True)})
    pf.assert_concept_visible(session, 1)  # must not raise
    pf.assert_concept_visible(_Session({}), 999)  # unknown id also fine


def test_registry_only_indicator_by_code_is_admitted():
    """Registry indicators have no local row; their gate is the registry itself."""
    from fd_open_data_mcp import public_face as pf

    pf.set_active_face("public")
    try:
        pf.assert_concept_visible(_Session({}), "SPY_ma5")  # non-numeric → admitted
    finally:
        pf.set_active_face("full")


def test_declaration_ships_inside_the_package():
    """The wheel is what runs in production; a path outside the package is absent there.

    This repo has already paid for this lesson once (price_sources/declarations.yaml
    was undeclared and the wheel shipped 0 yaml files). The failure mode here is a
    public face that refuses to start — intended, but discovered late.
    """
    from pathlib import Path

    import fd_open_data_mcp

    pkg_dir = Path(fd_open_data_mcp.__file__).resolve().parent
    decl = pkg_dir / "tool-surface.public.json"
    assert decl.is_file(), f"declaration must live inside the package: {decl}"
    # And the shipped default must point at exactly that file.
    from fd_open_data_mcp.public_face import DEFAULT_DECLARATION

    assert DEFAULT_DECLARATION.resolve() == decl.resolve()
