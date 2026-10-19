"""Tool behavior annotations contract (mcp-tool-annotations).

Three things must hold, or a client's auto-approval policy is built on sand:

1. **Completeness** — every tool in the RUNTIME registry carries all four
   hints. The registry is enumerated by building the server and listing its
   tools, never by counting decorators: what the server serves is the truth.
2. **Set equality** — the runtime names and ``TOOL_ANNOTATIONS`` are the same
   set. This is what catches a newly added tool that forgot its declaration
   (it would otherwise pass (1) via a silent default).
3. **Agreement with the implementation** — no tool that writes persistent
   state may be marked read-only. The expected-write set below is written by
   hand from reading the implementations, so changing a tool's side effects
   without updating its declaration turns this file red.
"""
from __future__ import annotations

import asyncio

import pytest

from fd_open_data_mcp.annotations import TOOL_ANNOTATIONS, annotations_for
from fd_open_data_mcp.server import mcp

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

#: Tools that write persistent state (a DB row, a config row, an execution or
#: event row, or the per-(scope, day) hit counters). This is the EXPECTED set,
#: derived by reading each implementation — not from the annotation table.
WRITES_STATE = {
    # catalog / semantic ingestion
    "import_catalog", "register_datasource", "register_discovered",
    "enumerate_wbgapi_indicators", "consume_concepts", "propose_bindings",
    "import_crosswalks", "record_concept_mapping", "confirm_binding",
    "update_entity", "update_concept", "update_binding",
    # entity identity / graph
    "seed_entity_identifiers", "add_entity_identifier", "add_entity",
    "add_relationship",
    # search: scoped variants record a per-(scope, day) hit counter
    "semantic_search", "semantic_search_unified", "ai_search",
    "list_registry_entries", "re_embed_concept",
    # ranking / planning lazily seed source_rankings rows
    "rank_sources", "plan_crawl", "policy_estimate",
    # retrieval scopes
    "scope_create", "scope_update", "scope_delete", "scope_bind_caller",
    "scope_unbind_caller",
    # concept-fetch: read-through cache writes, gap fill, forced refresh
    "read", "read_series", "fetch",
    # scheduled refresh
    "generate_refresh_schedules", "run_schedule",
    # crawl policy
    "policy_create", "policy_update", "policy_enable", "policy_disable",
    "policy_delete", "policy_trigger_now",
    # crawl platform
    "platform_trigger", "platform_cancel_run", "platform_cancel_pending",
    # identity pool: status applies the station timeout backstop
    "auth_status", "auth_request_login", "auth_launch_login",
}

#: Tools that can delete or irreversibly overwrite what a caller cannot recover.
DESTRUCTIVE = {"scope_delete", "policy_delete"}

#: Tools whose content comes from a live, non-enumerable external source.
OPEN_WORLD = {
    "read", "read_series", "fetch", "run_schedule", "enumerate_wbgapi_indicators",
}

#: Tools that append a new row per call (or reject the second call), so a
#: replay is not a no-op even though the first call's state change persists.
NOT_IDEMPOTENT = {
    "add_entity", "add_relationship", "scope_create", "scope_unbind_caller",
    "scope_delete", "policy_delete", "fetch", "run_schedule", "policy_create",
    "policy_trigger_now", "platform_trigger", "platform_cancel_run",
    "platform_cancel_pending", "auth_request_login", "auth_launch_login",
}


@pytest.fixture(scope="module")
def runtime() -> dict[str, dict]:
    """The four hints exactly as ``tools/list`` reports them."""
    out: dict[str, dict] = {}
    for t in asyncio.run(mcp.list_tools()):
        a = t.annotations
        assert a is not None, f"{t.name} declares no behavior annotations"
        out[t.name] = {k: getattr(a, k) for k in HINTS}
    return out


def test_every_registered_tool_declares_all_four_hints(runtime):
    assert runtime, "runtime registry is empty"
    for name, a in runtime.items():
        for k in HINTS:
            assert a[k] is not None, f"{name}.{k} is not declared"


def test_registry_and_annotation_table_are_the_same_set(runtime):
    assert set(runtime) == set(TOOL_ANNOTATIONS), (
        "runtime registry and annotations.TOOL_ANNOTATIONS disagree — "
        f"runtime-only: {sorted(set(runtime) - set(TOOL_ANNOTATIONS))}; "
        f"table-only: {sorted(set(TOOL_ANNOTATIONS) - set(runtime))}"
    )


def test_unknown_tool_raises_instead_of_defaulting():
    with pytest.raises(KeyError):
        annotations_for("no_such_tool")


def test_writing_tools_are_not_marked_read_only(runtime):
    for name in WRITES_STATE:
        assert name in runtime, f"{name} is in the expected-write set but not registered"
        assert runtime[name]["readOnlyHint"] is False, (
            f"{name} writes persistent state but is marked read-only — "
            "a client would auto-approve it"
        )


def test_readers_are_read_only_and_idempotent(runtime):
    readers = set(runtime) - WRITES_STATE
    assert readers, "the read-only family is empty — WRITES_STATE overreaches"
    for name in readers:
        assert runtime[name]["readOnlyHint"] is True, f"{name} reads but is not marked read-only"
        assert runtime[name]["idempotentHint"] is True, f"{name} reads but is not marked idempotent"


def test_destructive_matches_the_delete_tools(runtime):
    marked = {n for n, a in runtime.items() if a["destructiveHint"]}
    assert marked == DESTRUCTIVE, (
        "destructiveHint must mark exactly the delete tools — "
        f"marked: {sorted(marked)}, expected: {sorted(DESTRUCTIVE)}"
    )


def test_open_world_matches_live_external_content(runtime):
    marked = {n for n, a in runtime.items() if a["openWorldHint"]}
    assert marked == OPEN_WORLD, (
        "openWorldHint must mark exactly the tools serving live external "
        f"content — marked: {sorted(marked)}, expected: {sorted(OPEN_WORLD)}"
    )


def test_non_idempotent_matches_the_append_and_reject_tools(runtime):
    marked = {n for n, a in runtime.items() if not a["idempotentHint"]}
    assert marked == NOT_IDEMPOTENT, (
        "non-idempotent tools must be exactly the append/reject family — "
        f"marked: {sorted(marked)}, expected: {sorted(NOT_IDEMPOTENT)}"
    )


def test_no_writing_tool_is_both_read_only_and_destructive(runtime):
    """The two flags contradict each other; the table must never say both."""
    for name, a in runtime.items():
        assert not (a["readOnlyHint"] and a["destructiveHint"]), name


def test_annotations_are_per_tool_copies():
    """Mutating one tool's flags must not leak into another's."""
    a = annotations_for("scope_delete")
    a["readOnlyHint"] = True
    assert annotations_for("scope_create")["readOnlyHint"] is False
    assert annotations_for("scope_delete")["readOnlyHint"] is False
