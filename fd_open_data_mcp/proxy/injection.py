"""Source-agnostic per-fetch proxy injection (slim shim).

In the standalone-proxy-service design (openspec change ``add-proxy-service``),
this module is a thin shim. Per-source selection no longer lives here — it lives
in the standalone forwarder (``fd-proxy-service``, the ``proxy-fw`` pod). The
crawler calls ``proxy_client.acquire(source)`` per fetch -> the forwarder
returns an ``upstream_url`` -> ``use_proxy(upstream_url)`` injects it into
requests/httpx -> the crawler fetches through it (terminating TLS itself) ->
classifies via ``ban_rules`` -> ``proxy_client.release(...)``.

Why acquire/release and not blanket ``HTTP_PROXY``/``HTTPS_PROXY`` env: the
crawler's HTTP client terminates TLS, so only *it* can see the decrypted
response needed to classify a ban (status 403, body "too many requests"). A dumb
TCP-relay forward proxy on ``CONNECT`` cannot inspect HTTPS responses. The
acquire/release contract puts *selection* at the forwarder and *classification*
at the crawler — both see what they need. See
``fd_proxy_service/forwarder/server.py`` module docstring for the decision.

Ships-dark: if ``FD_PROXY_FORWARDER`` is unset (local dev / tests),
``proxy_client.acquire`` returns a direct sentinel (``upstream_url=None``) and
``release`` is a no-op — identical to today's ``scheme='direct'`` (no forwarder
running, egress direct from the worker's own node IP).

The contextvar + requests/httpx monkey-patches are unchanged: they are the
"inject upstream_url into the crawler's own HTTP client" mechanism. The
forwarder hands back a URL; this shim makes requests/httpx route through it.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# The proxy dict to pass to requests/httpx for the current fetch, or None.
_proxy_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_fd_proxy", default=None
)

_INSTALLED = False


def proxy_url(proxy) -> Optional[str]:
    """Build a proxy URL string for a Proxy model, or None for scheme=direct.

    Kept for callers that pass a ``Proxy`` model (probe/job.py, tests). The
    forwarder path passes a URL string straight into ``use_proxy``.
    """
    if proxy is None or proxy.scheme == "direct":
        return None
    auth = f"{proxy.auth}@" if getattr(proxy, "auth", None) else ""
    host = proxy.ip
    port = f":{proxy.port}" if proxy.port else ""
    scheme = "http" if proxy.scheme in ("http", "https") else proxy.scheme
    return f"{scheme}://{auth}{host}{port}"


def proxy_dict(proxy) -> Optional[dict]:
    """The ``proxies=`` dict for requests, or None for direct (no injection).

    Accepts a URL string (forwarder path), a Proxy-like model, or None.
    """
    if proxy is None:
        return None
    if isinstance(proxy, str):
        # forwarder path: a URL string (empty = direct)
        return None if not proxy else {"http": proxy, "https": proxy}
    url = proxy_url(proxy)
    return None if url is None else {"http": url, "https": url}


@contextmanager
def use_proxy(proxy):
    """Set the proxy for the duration of the block.

    Accepts a URL string (forwarder path), a Proxy model (probe/job.py path), or
    None for direct egress.
    """
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


# ---------------------------------------------------------------------------
# Forwarder client — acquire/release contract over HTTP
# ---------------------------------------------------------------------------


@dataclass
class Acquisition:
    """A forwarder acquire result.

    Mirrors ``fd_proxy_service.providers.base.Acquisition``. ``upstream_url`` is
    ``None`` for direct egress (ships-dark OR no healthy upstream); ``addr_id``
    is ``None`` when there is no circuit to update (direct sentinel).
    """
    upstream_url: Optional[str]
    addr_id: Optional[int]
    provider: Optional[str]


# Sentinel returned on ships-dark (no forwarder configured) or forwarder failure.
# Reused to avoid per-fetch allocation in the common direct-egress path.
_DIRECT_ACQ = Acquisition(upstream_url=None, addr_id=None, provider=None)


class _ProxyClient:
    """HTTP client for the standalone forwarder's acquire/release contract.

    Talks to ``$FD_PROXY_FORWARDER`` (e.g. ``http://proxy-fw.scraw:8080``) over
    stdlib ``urllib`` — NOT requests/httpx — so the call is never itself proxied
    (avoids recursing through the monkey-patches above). Ships-dark on any
    failure: ``acquire`` returns a direct sentinel, ``release`` is a no-op —
    fetches still succeed, just without rotation (matches ``circuit.py``'s
    graceful-degradation property).
    """

    def __init__(self, base_url: Optional[str] = None) -> None:
        self._override = base_url

    @property
    def _base(self) -> str:
        # Read lazily so a test that sets FD_PROXY_FORWARDER after import works,
        # and so a process picks up the env at call time, not import time.
        return (self._override or os.environ.get("FD_PROXY_FORWARDER") or "").rstrip("/")

    @property
    def enabled(self) -> bool:
        """True when a forwarder URL is configured (i.e. not ships-dark)."""
        return bool(self._base)

    def _post(self, path: str, payload: dict, timeout: float = 2.0) -> Optional[dict]:
        base = self._base
        if not base:
            return None
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{base}{path}", data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            logger.warning("proxy_client %s failed: %s — egressing direct", path, e)
            return None

    def acquire(self, source: str,
                exclude: Optional[list[int]] = None) -> Acquisition:
        """``POST /acquire {source, exclude}`` -> Acquisition.

        Returns a direct sentinel (``upstream_url=None``) on ships-dark
        (forwarder unset) OR when the forwarder has no healthy upstream OR on
        any forwarder failure. In all three cases the caller egresses direct.

        ``exclude`` is a list of addr_ids already tried in this
        ``instrumented_fetch`` call — the forwarder skips them so a dead proxy
        is not re-acquired within one fetch's retry loop (Bug 5). Empty/absent
        = no exclusion (degrades to today's behavior on an old forwarder that
        ignores the field — see design.md risk R5/R6).
        """
        body = self._post("/acquire", {"source": source,
                                       "exclude": exclude or []})
        if body is None:
            return _DIRECT_ACQ
        return Acquisition(
            upstream_url=body.get("upstream_url"),
            addr_id=body.get("addr_id"),
            provider=body.get("provider"),
        )

    def release(self, source: str, addr_id: Optional[int],
                provider: Optional[str], outcome: str) -> None:
        """``POST /release``. Best-effort (never raises).

        No-op when the forwarder is unset (ships-dark) or ``addr_id`` is None
        (direct sentinel — no circuit to update; matches the forwarder's own
        ``registry.release`` which returns early on ``addr_id is None``).
        """
        if not self._base or addr_id is None:
            return
        self._post("/release", {"source": source, "addr_id": addr_id,
                                "provider": provider, "outcome": outcome})


proxy_client = _ProxyClient()
