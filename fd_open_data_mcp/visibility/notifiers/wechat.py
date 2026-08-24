"""ServerChan (Server酱) notifier sink — v1 transport (add-crawl-visibility).

POSTs ``title``/``des`` to ``sctapi.ftqq.com/<token>.send`` so a message lands
in the operator's personal WeChat (official-account message). Zero-setup for an
individual: scan a QR once at https://sct.ftqq.com to mint the token.

The free tier is rate-limited (~several msgs/day). The watcher is engineered
around that: failure pushes are batched per scan window and the digest is one
msg/day, so normal operation is ~1 msg/day. A sink never raises — a missing
token or transport error logs and no-ops so the watcher keeps advancing.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ServerChan fields: `title` (≤32) and `des` (the body, ≤32KB). We keep the
# body well under the cap; the digest/failure messages are a few hundred bytes.
_TITLE_MAX = 32
_DES_MAX = 32_000
_ENDPOINT = "https://sctapi.ftqq.com/{token}.send"


class ServerChanNotifier:
    """ServerChan transport. Token from ``SCRAW_WATCHER_SCT_TOKEN``."""

    def __init__(self, token: str | None = None, timeout: float = 10.0):
        self.token = (token or os.environ.get("SCRAW_WATCHER_SCT_TOKEN") or "").strip()
        self.timeout = timeout

    def send(self, title: str, body: str, *, level: str = "info") -> None:
        if not self.token:
            logger.warning(
                "ServerChan: SCRAW_WATCHER_SCT_TOKEN unset — notification not sent "
                "(title=%r). Watcher continues; set the token to enable WeChat delivery.",
                title,
            )
            return
        t = (title or "")[:_TITLE_MAX]
        b = (body or "")[:_DES_MAX]
        url = _ENDPOINT.format(token=self.token)
        data = urllib.parse.urlencode({"title": t, "des": b}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - https endpoint
                payload = json.loads(r.read().decode("utf-8", "replace"))
            # ServerChan returns {"code": 0} on success; non-zero is a delivery fault.
            if payload.get("code") not in (0, None):
                logger.warning("ServerChan delivery returned non-zero: %s", payload)
            else:
                logger.info("ServerChan sent: %s", t)
        except Exception as e:  # noqa: BLE001 - a sink must never crash the watcher
            logger.warning("ServerChan send failed (title=%r): %s", t, e)


class NullNotifier:
    """No-op sink (used when ``SCRAW_NOTIFIER`` is unset/unknown).

    Logs the notification so the watcher's stdout still shows what *would* have
    been pushed — useful for dry-runs and tests.
    """

    def send(self, title: str, body: str, *, level: str = "info") -> None:
        logger.info("[null-notifier] %s\n%s", title, body)
