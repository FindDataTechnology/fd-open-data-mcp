"""Sources board humanized schedule times (panel-rbac-i18n-refresh task 5.1).

Spec: run timestamps render in Asia/Shanghai at second precision with a
relative hint (no raw UTC ISO/microseconds), cron schedules render
human-readable with the projected next fire — verified against the exact
production shapes from the user's screenshots (`23 3 * * *`,
`2026-10-07T05:29:09.000946+00:00`-style values).
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.models import CrawlRun, CrawlSite, CrawlSource
from fd_open_data_mcp.panel import app as appmod


@pytest.fixture
def open_env(monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID", "LOGTO_CLIENT_SECRET",
              "PANEL_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _client():
    return TestClient(appmod.app, follow_redirects=False)


def _seed(session):
    session.add(CrawlSite(id="demo"))
    session.add(CrawlSource(source="demo-src", site="demo",
                            schedule="23 3 * * *", enabled=True))
    session.add(CrawlRun(source="demo-src", status="success",
                         started_at=dt.datetime(2026, 10, 7, 5, 29, 9, 946),
                         rows_written=123))
    session.commit()


def test_sources_renders_humanized_cron_and_local_time(session, open_env):
    _seed(session)
    html = _client().get("/panel/sources").text
    assert "每天 03:23" in html           # cron humanized (zh default)
    assert "下次" in html                 # projected next fire present
    assert "2026-10-07 13:29:09" in html  # Beijing local, second precision
    assert "+00:00" not in html           # no raw offset
    assert ".000946" not in html          # no microseconds
    # the raw expression stays available as a tooltip, not the cell content
    assert 'title="23 3 * * *"' in html


def test_lang_toggle_roundtrip(session, open_env):
    _seed(session)
    c = _client()
    r = c.get("/panel/lang", params={"to": "en", "back": "/panel/sources"})
    assert r.status_code == 303 and r.headers["location"] == "/panel/sources"
    assert r.cookies.get("panel-lang") == "en"
    # open redirect refused: only in-app paths
    r2 = c.get("/panel/lang", params={"to": "en",
                                      "back": "https://evil.example"})
    assert r2.headers["location"] == "/panel"
    # english renders humanized cron in english
    html = c.get("/panel/sources", cookies={"panel-lang": "en"}).text
    assert "daily 03:23" in html
    assert "next" in html
    assert "每天" not in html
