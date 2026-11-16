"""Custom exceptions for fd-open-data-mcp."""

from typing import Optional


class FetchError(Exception):
    """Raised when an upstream call cannot be made or fails.

    **This is the only ``FetchError`` in the package.** It used to exist twice —
    here (carrying ``source``/``command``) and in ``fetch/runner.py`` (carrying
    ``status_code``/``response_text``) — as two unrelated classes. The adapters
    importing the one here (``nbs_gdp``, ``datacommons``) therefore raised an
    exception that ``except FetchError`` in ``fetch/dispatch.py`` and
    ``fetch/instrumentation.py`` did **not** catch: those failures skipped the
    fetch_log error row and the retry/ban classification. ``fetch.runner`` now
    re-exports this class, so both import paths name the same object.

    The attributes serve different consumers: ``status_code``/``response_text``
    feed ``ban_rules.classify`` (HTTP 403/429/captcha detection; both ``None``
    for connection-level errors, where there is no response to inspect), while
    ``source``/``command`` name the coordinates for human-facing messages.
    """

    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_text: Optional[str] = None,
                 source: Optional[str] = None, command: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text
        self.source = source
        self.command = command

    def __str__(self):
        if self.source and self.command:
            return f"{self.source}.{self.command}: {super().__str__()}"
        return super().__str__()


class FinalizedObservationError(Exception):
    """Raised when a write would overwrite an observation past its revision window.

    落实 ``fact-ledger-kernel`` R1（事实只追加不得改写）在观测接入面的具体形态：
    定型后的值不可被普通写入改写。确需更正时走 correction 通道（显式声明 + 理由），
    该通道照常落 ``observation_revisions`` 留痕。

    拒绝必须**指名坐标**——只报"有若干条已定型"等于让人自己去猜。
    """

    def __init__(self, message: str, coordinates: list[tuple] = None,
                 window: dict = None):
        super().__init__(message)
        self.coordinates = coordinates or []
        self.window = window or {}
