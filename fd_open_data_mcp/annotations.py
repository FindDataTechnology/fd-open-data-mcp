"""MCP tool behavior annotations (mcp-tool-annotations).

Every tool this server registers declares the four MCP hints here, once. The
``@mcp.tool(annotations=annotations_for(...))`` wiring reads this table, and
``tests/test_tool_annotations.py`` compares the table against the RUNTIME
registry (build the server, list tools) — so a newly added tool that forgets
its flags fails the test instead of silently defaulting.

Judgment rules used below:

* ``readOnlyHint`` — False whenever the tool can write a persistent row:
  ingestion/upsert/corrections, cache warming, lazy seed rows, audit/execution
  rows, and the per-(scope, day) hit counters the scoped search/read tools
  record. A tool that writes is NOT read-only, even when the write is an
  opportunistic side effect of an otherwise read-shaped call.
* ``destructiveHint`` — True only for the two delete tools: a deleted scope or
  policy cannot be recovered. In-place corrections and idempotent upserts are
  False — re-running the same payload reproduces the same row (the rule's
  explicit "idempotent upsert" case).
* ``idempotentHint`` — True when repeating the same call converges to the same
  data state (upserts keyed on a natural key, insert-only gap fills, CAS
  flags). False for append-only writers (a new execution/pending/event row per
  call) and for creates whose second call is rejected. Monotonic telemetry
  counters (scope hit stats) are excluded from this judgment: they are
  observability, not the tool's data, so a repeated search remains retry-safe.
* ``openWorldHint`` — True only where the returned CONTENT comes from a live,
  non-enumerable external source: upstream data APIs (``read`` / ``fetch`` /
  ``read_series`` / ``run_schedule``) and the wbgapi indicator enumeration.
  Queries over this service's own versioned database, control-plane operations
  on the operator's own fleet, and local registry/rule files are False.
"""
from __future__ import annotations

from typing import Any

# ─── flag profiles ──────────────────────────────────────────────────────────

#: Pure read: no persistent write, closed world, retry-safe.
READ_ONLY: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: Writes persistent state but converges on re-run (upsert / correction /
#: seed-if-absent). Not destructive: the same payload reproduces the same row.
WRITE_IDEMPOTENT: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: Writes and appends a new row per call (or rejects the second call).
WRITE_NOT_IDEMPOTENT: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

#: Deletes rows a caller cannot recover (the only two delete tools).
WRITE_DESTRUCTIVE: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}

#: Writes, converges, and serves content from a live upstream source.
WRITE_OPEN_WORLD_IDEMPOTENT: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Writes an execution record per call and serves live upstream content.
WRITE_OPEN_WORLD_NOT_IDEMPOTENT: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


# ─── the table ──────────────────────────────────────────────────────────────
# Grouped by the module that registers the tool (server.py unless noted).
TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    # ── catalog (server.py) ────────────────────────────────────────────────
    "import_catalog": dict(WRITE_IDEMPOTENT),  # upserts sources/functions/columns
    "register_datasource": dict(WRITE_IDEMPOTENT),  # upserts a manifest
    "register_discovered": dict(WRITE_IDEMPOTENT),  # upserts discovered manifests
    "enumerate_wbgapi_indicators": dict(WRITE_OPEN_WORLD_IDEMPOTENT),  # wb.series.info() is live
    "list_cnreport_rules": dict(READ_ONLY),  # reads fd-cn-report's sqlite in ro mode

    # ── semantic layer (server.py) ─────────────────────────────────────────
    "consume_concepts": dict(WRITE_IDEMPOTENT),  # seeds families/variables
    "propose_bindings": dict(WRITE_IDEMPOTENT),  # adds missing bindings only
    "list_concepts": dict(READ_ONLY),
    "list_concept_families": dict(READ_ONLY),
    "list_registry_entries": dict(WRITE_IDEMPOTENT),  # scoped call records a hit counter
    "record_concept_mapping": dict(WRITE_IDEMPOTENT),  # upsert on the 4-field key
    "list_concept_mappings": dict(READ_ONLY),
    "import_crosswalks": dict(WRITE_IDEMPOTENT),  # upserts registry assertions
    "list_bindings": dict(READ_ONLY),
    "review_bindings": dict(READ_ONLY),
    "confirm_binding": dict(WRITE_IDEMPOTENT),  # marks reviewed; same result on re-run
    "update_entity": dict(WRITE_IDEMPOTENT),  # correction/upsert, same payload same row
    "update_concept": dict(WRITE_IDEMPOTENT),
    "update_binding": dict(WRITE_IDEMPOTENT),

    # ── entity identity / graph (server.py) ────────────────────────────────
    "seed_entity_identifiers": dict(WRITE_IDEMPOTENT),  # bulk upsert
    "resolve_entity": dict(READ_ONLY),
    "add_entity_identifier": dict(WRITE_IDEMPOTENT),  # upsert
    "list_entities": dict(READ_ONLY),
    "get_entity": dict(READ_ONLY),
    "add_entity": dict(WRITE_NOT_IDEMPOTENT),  # second insert raises "already exists"
    "list_relationships": dict(READ_ONLY),
    "add_relationship": dict(WRITE_NOT_IDEMPOTENT),  # second insert raises "already exists"
    "graph_search": dict(READ_ONLY),  # in-memory graph cache only

    # ── search (server.py) ─────────────────────────────────────────────────
    # The scoped variants record a per-(scope, day) hit counter: that is a
    # persistent write, so they are not read-only. The counter is telemetry —
    # the returned ranking is unchanged — hence idempotent stays True.
    "semantic_search": dict(WRITE_IDEMPOTENT),
    "semantic_search_entities": dict(READ_ONLY),  # no scope parameter, no writes
    "semantic_search_unified": dict(WRITE_IDEMPOTENT),
    "ai_search": dict(WRITE_IDEMPOTENT),
    "re_embed_concept": dict(WRITE_IDEMPOTENT),  # rewrites the same embedding row

    # ── source ranking (server.py) ─────────────────────────────────────────
    # rank_sources lazily seeds source_rankings rows for the concept's bound
    # sources (ensure_rankings_for_concept commits); the seed is insert-only.
    "rank_sources": dict(WRITE_IDEMPOTENT),

    # ── retrieval scopes (server.py) ───────────────────────────────────────
    "scope_create": dict(WRITE_NOT_IDEMPOTENT),  # second create rejects "already exists"
    "scope_list": dict(READ_ONLY),
    "scope_update": dict(WRITE_IDEMPOTENT),
    "scope_delete": dict(WRITE_DESTRUCTIVE),  # drops the scope + its caller bindings
    "scope_bind_caller": dict(WRITE_IDEMPOTENT),  # upsert
    "scope_unbind_caller": dict(WRITE_NOT_IDEMPOTENT),  # second call errors "no binding"
    "scope_list_bindings": dict(READ_ONLY),
    "scope_stats": dict(READ_ONLY),

    # ── concept-fetch (server.py) ──────────────────────────────────────────
    # read is a read-through cache: a miss/stale row dispatches upstream and
    # writes the observation (plus fetch_log/ranking counters). Repeated calls
    # converge once the row is fresh. Registry-only reads go through the
    # business-mcp federation channel, a live external service.
    "read": dict(WRITE_OPEN_WORLD_IDEMPOTENT),
    # read_series serves stored cache only, but fill_gaps=True fetches and
    # inserts the missing intervals (insert-only), and registry reads federate.
    "read_series": dict(WRITE_OPEN_WORLD_IDEMPOTENT),
    "fetch": dict(WRITE_OPEN_WORLD_NOT_IDEMPOTENT),  # force-refetch + new Execution row
    "plan_crawl": dict(WRITE_IDEMPOTENT),  # compiles a plan; lazily seeds ranking rows

    # ── scheduled refresh (server.py) ──────────────────────────────────────
    "generate_refresh_schedules": dict(WRITE_IDEMPOTENT),  # create/update per concept
    "list_schedules": dict(READ_ONLY),
    "run_schedule": dict(WRITE_OPEN_WORLD_NOT_IDEMPOTENT),  # last_run_at + Execution per call

    # ── crawl policy (policy_tools.py) ─────────────────────────────────────
    "policy_create": dict(WRITE_NOT_IDEMPOTENT),  # unique name; second call rejected
    "policy_list": dict(READ_ONLY),
    "policy_get": dict(READ_ONLY),
    "policy_update": dict(WRITE_IDEMPOTENT),
    "policy_enable": dict(WRITE_IDEMPOTENT),
    "policy_disable": dict(WRITE_IDEMPOTENT),
    "policy_delete": dict(WRITE_DESTRUCTIVE),  # cascades to the policy's runs
    "policy_trigger_now": dict(WRITE_NOT_IDEMPOTENT),  # new PolicyRun per launch
    "policy_runs": dict(READ_ONLY),
    "policy_estimate": dict(WRITE_IDEMPOTENT),  # plan_crawl lazily seeds ranking rows
    "data_stats": dict(READ_ONLY),  # census/coverage caches are read, never collected

    # ── crawl platform (platform_tools.py) ─────────────────────────────────
    "platform_sources": dict(READ_ONLY),
    "platform_runs": dict(READ_ONLY),
    "platform_trigger": dict(WRITE_NOT_IDEMPOTENT),  # inserts a pending_runs row per call
    # CAS flag/status writes: the state converges, but the SECOND call returns
    # "already_finished" instead of "cancel_requested"/"cancelled" — a different
    # result, so replay is not safe to treat as a no-op.
    "platform_cancel_run": dict(WRITE_NOT_IDEMPOTENT),
    "platform_cancel_pending": dict(WRITE_NOT_IDEMPOTENT),

    # ── crawl visibility (visibility_tools.py) ─────────────────────────────
    "crawl_status": dict(READ_ONLY),

    # ── coverage (coverage_tools.py) ───────────────────────────────────────
    "coverage_report": dict(READ_ONLY),

    # ── identity pool (auth_tools.py) ──────────────────────────────────────
    # auth_status applies the station deadline backstop: expired stations are
    # closed and evented, so it can write even though it is a status read.
    "auth_status": dict(WRITE_IDEMPOTENT),
    "auth_events": dict(READ_ONLY),
    "auth_request_login": dict(WRITE_NOT_IDEMPOTENT),  # appends a note event per call
    "auth_launch_login": dict(WRITE_NOT_IDEMPOTENT),  # creates a station + Job per call
}


def annotations_for(name: str) -> dict[str, Any]:
    """The four hints for ``name`` as a fresh copy.

    An unknown name raises ``KeyError``: a silent default would let a tool that
    forgot its declaration pass the runtime contract test.
    """
    try:
        return dict(TOOL_ANNOTATIONS[name])
    except KeyError:
        raise KeyError(
            f"tool {name!r} has no entry in annotations.TOOL_ANNOTATIONS"
        ) from None
