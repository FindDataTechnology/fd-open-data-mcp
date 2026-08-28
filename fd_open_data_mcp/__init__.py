"""fd-open-data-mcp: open-data ontology MCP.

A semantic concept layer over multi-datasource financial/economic data.
Consumes the fd-* datasource registries and fd-entities-indicators read-only,
and adds: a unified catalog, concept<->column bindings, cross-source entity
identity, ranked dispatch, a read-through concept-keyed cache, and
frequency-driven auto-refresh.
"""
__version__ = "0.5.1"
