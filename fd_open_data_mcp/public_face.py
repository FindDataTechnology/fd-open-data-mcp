"""Tool-face selection at the registration layer (openspec: public-read-surface).

One service, several faces. `FACET_OPS_FACE=registry|market` already proves the
shape in this fleet: the same image runs twice and each deployment exposes only
its own declared surface. This module brings a third face — `public` — to the
open-data server so an outward-facing agent (the product-intro copilots) can
actually read the catalog instead of only describing it.

Why selection happens HERE and not inside each tool: a call-time check produces
"the tool is listed but every call is refused", which misleads both the visitor
and the model — and it leaks the tool NAMES into the public answer first, which
is the whole thing the public face exists to prevent.

Honesty rules (spec: public-read-surface):

1. The declaration file is the only source of truth for the public face.
2. A declared tool the running build does not register is a STARTUP FAILURE, not
   a silent skip — a shrunken surface nobody notices is exactly how the
   `-32004 cold start` class of bug hides.
3. An empty declaration means ZERO tools, never "no filtering = everything".
   The same empty value already means the opposite elsewhere in this stack
   (an empty `mcpServers` = no narrowing), and one value must not carry two
   meanings.

The tool inventory is read from the FastMCP provider at call time, NOT from a
hand-maintained list: the static grep of `@mcp.tool` in server.py found 50 while
the live registration holds 87 (the `policy_*` / `platform_*` / `restaurant_*`
families register from their own modules). A list built from one file would have
been wrong in the direction that matters — silently offering a face that is not
what was declared.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

#: Environment variable that selects the face. Absent = the historical behavior
#: (every tool registered), byte-for-byte.
FACE_ENV = "FD_OPEN_DATA_FACE"

#: Faces this server knows how to run. `full` is the implicit default.
FACES = ("full", "public")

#: The declaration file, shipped INSIDE the package so a source checkout and an
#: installed wheel resolve the same path. It must be declared in
#: `[tool.setuptools.package-data]` — an undeclared data file is absent from the
#: wheel, and the face then refuses to start (which is the intended failure: a
#: silently shrunken public face is worse than no public face).
DEFAULT_DECLARATION = Path(__file__).resolve().parent / "tool-surface.public.json"


class FaceConfigError(RuntimeError):
    """A misconfigured face. Raised at startup so the process refuses to serve."""


#: The face this process is actually serving. `full` until `apply_face` says
#: otherwise; read tools consult it to narrow the DATA range, not just the tool
#: list (spec: "只读不等于可公开").
_ACTIVE_FACE = "full"


def active_face() -> str:
    """The face this process is serving."""
    return _ACTIVE_FACE


def set_active_face(face: str) -> None:
    global _ACTIVE_FACE
    if face not in FACES:
        raise FaceConfigError(f"unsupported face {face!r}")
    _ACTIVE_FACE = face


def resolve_face(raw: str | None = None) -> str:
    """The configured face. Absent/blank = `full` (the pre-change behavior)."""
    value = (raw if raw is not None else os.environ.get(FACE_ENV, "")).strip().lower()
    if not value:
        return "full"
    if value not in FACES:
        raise FaceConfigError(
            f"{FACE_ENV}={value!r} is not a known face; valid values: {', '.join(FACES)}"
        )
    return value


def load_public_declaration(path: Path | None = None) -> dict:
    """Read and shape-check the public face declaration."""
    target = path or DEFAULT_DECLARATION
    try:
        doc = json.loads(Path(target).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FaceConfigError(
            f"public face declared but the declaration file is missing: {target}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FaceConfigError(f"declaration is not valid JSON: {target}: {exc}") from exc

    if not isinstance(doc, dict):
        raise FaceConfigError(f"declaration must be a JSON object: {target}")
    if doc.get("schema") != "mcp-tool-surface/v1":
        raise FaceConfigError(
            f"declaration schema must be 'mcp-tool-surface/v1', got {doc.get('schema')!r}"
        )

    tools = doc.get("tools")
    if not isinstance(tools, list):
        raise FaceConfigError("declaration must carry a 'tools' array")

    seen: set[str] = set()
    for entry in tools:
        if not isinstance(entry, dict):
            raise FaceConfigError(f"every tool entry must be an object, got {type(entry).__name__}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise FaceConfigError("every tool entry needs a non-empty 'name'")
        if name in seen:
            raise FaceConfigError(f"tool {name!r} is declared twice")
        seen.add(name)
        # The reason is not decoration: it is what a reviewer reads instead of
        # the implementation, so a missing one is a hard error.
        reason = entry.get("why")
        if not isinstance(reason, str) or not reason.strip():
            raise FaceConfigError(f"tool {name!r} needs a non-empty 'why' (why may visitors see it)")

    return doc


def declared_tool_names(declaration: dict) -> list[str]:
    return [entry["name"] for entry in declaration["tools"]]


def check_declared_against(declared: Iterable[str], registered: Iterable[str]) -> None:
    """Every declared name must exist in the running build's registration.

    Ghost declarations fail here rather than at the first visitor's question.
    """
    have = set(registered)
    unknown = [n for n in declared if n not in have]
    if unknown:
        raise FaceConfigError(
            "declaration names tools this build does not register: "
            + ", ".join(sorted(unknown))
            + " — either implement them or remove them from the declaration (a silently "
            "shrunken public face is how 'the copilot answers nothing' stays invisible)"
        )


# ── FastMCP plumbing ────────────────────────────────────────────────────────
# The provider exposes list_tools / remove_tool as its own API; the tool store
# lives on `mcp._local_provider` in the pinned fastmcp build. Both are probed
# defensively: if the shape moves, selection must FAIL rather than serve an
# unknown surface.

def _provider(mcp):
    for attr in ("_local_provider", "local_provider"):
        provider = getattr(mcp, attr, None)
        if provider is not None:
            return provider
    raise FaceConfigError(
        "this FastMCP build exposes no local provider; face selection cannot run "
        "(refusing to start rather than serving an unknown surface)"
    )


def registered_tool_names(mcp) -> list[str]:
    """The tool names the instance currently registers, via the provider's API."""
    provider = _provider(mcp)
    lister = getattr(provider, "list_tools", None)
    if lister is None:
        raise FaceConfigError("provider has no list_tools; cannot enumerate the face")
    import asyncio

    tools = asyncio.run(lister())
    names = [getattr(t, "name", None) for t in tools]
    if any(n is None for n in names):
        raise FaceConfigError("a registered tool carries no name; refusing to guess the surface")
    return sorted(names)


def apply_face(mcp, face: str, declaration: dict | None = None) -> list[str]:
    """Drop every registered tool the face may not expose; return what survived."""
    current = registered_tool_names(mcp)
    if face == "full":
        set_active_face("full")
        return current

    doc = declaration if declaration is not None else load_public_declaration()
    declared = declared_tool_names(doc)
    check_declared_against(declared, current)

    provider = _provider(mcp)
    remove = getattr(provider, "remove_tool", None)
    if remove is None:
        raise FaceConfigError("provider has no remove_tool; cannot apply the face")

    keep = set(declared)
    removed: list[str] = []
    for name in current:
        if name in keep:
            continue
        remove(name)
        removed.append(name)

    set_active_face(face)

    after = set(registered_tool_names(mcp))
    if after != keep:
        leaked = sorted(after - keep)
        missing = sorted(keep - after)
        raise FaceConfigError(
            f"face {face!r} did not apply cleanly — "
            f"still exposed but undeclared: {leaked or 'none'}; "
            f"declared but absent: {missing or 'none'}"
        )
    return sorted(keep)


# ── Data-range narrowing (spec: "只读不等于可公开") ──────────────────────────
# The tool list is the first boundary; the DATA is the second. A read-only tool
# that happily returns an internal concept is the same leak with an extra step.

def assert_concept_visible(session, concept_id) -> None:
    """Refuse internal concepts on the public face.

    Only meaningful under the public face — every other face keeps the exact
    behavior it had before this module existed.

    The criterion is the registry's own `verified` flag with `deprecated`
    excluded (production: 2631 concepts, 1033 verified, 1607 deprecated). No new
    column: it would overlap an existing meaning and need a backfill.

    Registry-only indicators (id=None, addressed by semantic/native code) have no
    row to check, so they are admitted: they come from the indicator registry
    where `verified` is the gate for appearing at all.
    """
    if _ACTIVE_FACE != "public":
        return
    try:
        numeric = int(concept_id)
    except (TypeError, ValueError):
        return  # registry-only indicator addressed by code — see docstring
    from fd_open_data_mcp.models import Concept

    row = session.get(Concept, numeric)
    if row is None:
        raise PermissionError(
            f"concept {numeric} is not available on the public face (unknown id)"
        )
    if not getattr(row, "verified", False) or getattr(row, "deprecated", False):
        raise PermissionError(
            f"concept {numeric} is not available on the public face "
            "(only verified, non-deprecated concepts are public)"
        )
