"""Tests for FetchError structured attrs + _extract_http_attrs (Bug 4 / G3).

``FetchError`` carries optional ``status_code``/``response_text`` so
``ban_rules.classify`` can match status-based and body-based rules (HTTP
403/429/captcha). ``_extract_http_attrs`` best-effort extracts them from an
upstream exception's ``.response``; connection-level errors (no HTTP response)
return ``(None, None)`` -> classify defaults to transient.
"""
from fd_open_data_mcp.fetch.runner import FetchError, _extract_http_attrs


class _FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FakeHttpError(Exception):
    """Mimics requests.HTTPError / httpx.HTTPStatusError (has a .response)."""
    def __init__(self, response):
        super().__init__("http error")
        self.response = response


def test_fetch_error_carries_status_code_and_body():
    e = FetchError("rate limited", status_code=429, response_text="too many requests")
    assert e.status_code == 429
    assert e.response_text == "too many requests"
    assert str(e) == "rate limited"


def test_fetch_error_defaults_to_none():
    e = FetchError("boom")
    assert e.status_code is None
    assert e.response_text is None


def test_extract_http_attrs_from_response_bearing_exception():
    e = _FakeHttpError(_FakeResponse(403, "forbidden"))
    status, text = _extract_http_attrs(e)
    assert status == 403
    assert text == "forbidden"


def test_extract_http_attrs_no_response_attr():
    # A plain connection error (socket / RemoteDisconnected / timeout) carries
    # no .response -> (None, None) -> classify falls back to transient default.
    e = ConnectionError("RemoteDisconnected('Remote end closed connection without response')")
    status, text = _extract_http_attrs(e)
    assert status is None
    assert text is None


def test_extract_http_attrs_response_is_none():
    e = _FakeHttpError(None)
    status, text = _extract_http_attrs(e)
    assert status is None
    assert text is None
