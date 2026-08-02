"""Source-agnostic per-fetch proxy injection.

A contextvar holds the proxy for the current fetch. Monkey-patched
``requests.Session.request`` and ``httpx.Client.request`` read it and inject
``proxies=`` so that akshare / yfinance / worldbank / wbgapi / ckan / nbs (all
``requests``-based) and edgar (``httpx``-based) route through the selected
upstream proxy WITHOUT any change to those libraries or to the adapters.

``scheme='direct'`` (the cluster's own egress) injects nothing - it is the
no-proxy default, ranked first so real proxies are only used once direct is
banned.

The patch is idempotent and installed once on first use. Per-fetch isolation is
guaranteed by ``contextvars``: concurrent in-process fetches each have their own
contextvar scope.
"""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# The proxy dict to pass to requests/httpx for the current fetch, or None.
_proxy_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_fd_proxy", default=None
)

_INSTALLED = False


def proxy_url(proxy) -> Optional[str]:
    """Build a proxy URL string for a Proxy model, or None for scheme=direct."""
    if proxy is None or proxy.scheme == "direct":
        return None
    auth = f"{proxy.auth}@" if getattr(proxy, "auth", None) else ""
    host = proxy.ip
    port = f":{proxy.port}" if proxy.port else ""
    scheme = "http" if proxy.scheme in ("http", "https") else proxy.scheme
    return f"{scheme}://{auth}{host}{port}"


def proxy_dict(proxy) -> Optional[dict]:
    """The ``proxies=`` dict for requests, or None for direct (no injection)."""
    url = proxy_url(proxy)
    if url is None:
        return None
    return {"http": url, "https": url}


@contextmanager
def use_proxy(proxy):
    """Set the proxy for the duration of the block."""
    token = _proxy_var.set(proxy_dict(proxy))
    try:
        yield
    finally:
        _proxy_var.reset(token)


def install() -> None:
    """Idempotently install the requests + httpx monkey-patches."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_requests_patch()
    _install_httpx_patch()
    _INSTALLED = True


def _install_requests_patch() -> None:
    try:
        import requests  # type: ignore
    except ImportError:
        return

    if getattr(requests.Session.request, "_fd_patched", False):
        return

    original = requests.Session.request

    def patched(self, method, url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        px = _proxy_var.get()
        if px is not None and "proxies" not in kwargs:
            kwargs["proxies"] = px
        return original(self, method, url, *args, **kwargs)

    patched._fd_patched = True  # type: ignore[attr-defined]
    requests.Session.request = patched
    logger.debug("requests proxy patch installed")


def _install_httpx_patch() -> None:
    try:
        import httpx  # type: ignore
    except ImportError:
        return

    if getattr(httpx.Client.request, "_fd_patched", False):
        return

    original = httpx.Client.request

    def patched(self, method, url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        px = _proxy_var.get()
        if px is None:
            return original(self, method, url, *args, **kwargs)
        # httpx does not accept per-request proxies on .request(); build a
        # one-off client through the selected proxy. Best-effort: if the httpx
        # version rejects the proxy kwarg, fall back to the original call (direct).
        proxy_url = px.get("https") or px.get("http")
        try:
            with httpx.Client(proxy=proxy_url, headers=getattr(self, "headers", None)) as c:
                return original(c, method, url, *args, **kwargs)
        except TypeError:
            # older httpx used `proxies=`; try that, else give up and go direct.
            try:
                with httpx.Client(proxies=px) as c:
                    return original(c, method, url, *args, **kwargs)
            except Exception:  # noqa: BLE001
                return original(self, method, url, *args, **kwargs)

    patched._fd_patched = True  # type: ignore[attr-defined]
    httpx.Client.request = patched
    logger.debug("httpx proxy patch installed")
