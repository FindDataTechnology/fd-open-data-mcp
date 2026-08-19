"""source-proxy-health proxy package — DEPRECATED compat shim.

The canonical home for proxy logic is now the standalone ``fd-proxy-service``
project (openspec change ``add-proxy-service``): the per-worker forwarder
(``proxy-fw``) owns selection + circuit state; the crawler talks to it via
``fd_open_data_mcp.proxy.injection.proxy_client`` (acquire/release contract).
This package remains as a thin compat layer for callers that still import its
submodules (``probe/job.py``, ``ranking/scorer.py``, ``refresh/reconciler.py``,
``cli.py``, ``scraw.concept_crawl_spider``, tests).

Transition state: the local submodule copies (``circuit``, ``ban_rules``,
``rate_limit``, ``pool``, ``seed``, ``selector``) are the WORKING implementation
during the cutover — they stay in place so external callers keep working without
a hard dependency on ``fd_proxy_service`` (which is NOT yet installed in the
scraw venv; a hard ``from fd_proxy_service ...`` here would break the crawler).
Once ``fd-proxy-service`` is installed in every venv, these local copies will be
deleted in favor of re-exports from ``fd_proxy_service`` (follow-up). Until then
a submodule re-export is deliberately NOT wired here, to avoid a split between
the package attribute (the re-exported copy) and the submodule file (the local
copy) in the same process.

``injection`` is the one module slimmed in THIS change: it now exposes
``proxy_client`` (acquire/release over HTTP to the forwarder) + ``use_proxy``
and no longer owns per-source selection. The other submodules are unchanged.
"""
from __future__ import annotations
