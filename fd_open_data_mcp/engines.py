"""Process-wide lazy singletons for the search engines.

Caches must survive across tool calls within their TTLs (spec semantic-search:
retrieval caches survive across calls). Both fd-open-data-mcp and
fd-find-data-business-mcp take their engines from here so the graph TTL and
the embedding query cache actually persist between calls. Write tools call
``invalidate_graph`` / ``invalidate_searches`` instead of waiting out the TTL;
``invalidate_searches`` also drops the transitional vector-matrix cache
(``fd_open_data_mcp.vector_backend``, FD_MCP_VECTOR_BACKEND=matrix).
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_graph_manager = None
_entity_search = None


def _graph_cache_ttl() -> int:
    return int(os.environ.get("GRAPH_CACHE_TTL", "300"))


def get_graph_manager():
    """The shared EntityGraphManager (GRAPH_CACHE_TTL wired)."""
    global _graph_manager
    if _graph_manager is None:
        with _lock:
            if _graph_manager is None:
                from fd_open_data_mcp.db import get_database
                from fd_open_data_mcp.graph.manager import EntityGraphManager
                _graph_manager = EntityGraphManager(
                    get_database().database_url, cache_ttl=_graph_cache_ttl()
                )
    return _graph_manager


def get_entity_search():
    """The shared EntitySemanticSearch (EMBEDDING_CACHE_SIZE wired)."""
    global _entity_search
    if _entity_search is None:
        with _lock:
            if _entity_search is None:
                from fd_open_data_mcp.db import get_database
                from fd_open_data_mcp.embeddings.model import MODEL_NAME
                from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch
                _entity_search = EntitySemanticSearch(
                    get_database().database_url, model_name=MODEL_NAME
                )
    return _entity_search


def invalidate_graph() -> None:
    """Drop the cached graph after entity/relationship writes."""
    with _lock:
        if _graph_manager is not None:
            _graph_manager.invalidate()


def invalidate_searches() -> None:
    """Reset query caches after concept/embedding writes."""
    with _lock:
        if _entity_search is not None:
            _entity_search.invalidate_cache()
    from fd_open_data_mcp import search_cache, vector_backend

    search_cache.invalidate()
    # The transitional vector matrix (FD_MCP_VECTOR_BACKEND=matrix) is a
    # snapshot of the embeddings table: writes make it stale, drop it now
    # instead of waiting out VECTOR_MATRIX_TTL.
    vector_backend.invalidate_matrix_cache()


def reset_engines() -> None:
    """Drop the singletons entirely (tests / DB switch)."""
    global _graph_manager, _entity_search
    with _lock:
        _graph_manager = None
        _entity_search = None
    from fd_open_data_mcp import vector_backend

    vector_backend.invalidate_matrix_cache()
