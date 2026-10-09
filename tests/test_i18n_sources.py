"""Sources/funnel/data group dictionary rendering (panel-rbac-i18n-refresh 4.3).

The group templates now route their copy through ``t()``; the en locale
must read pure English from the merged group dictionary
(``i18n_en_sources.py``) with no mixed 中文 runs, while the zh locale keeps
rendering the verbatim bilingual chrome (existing assertions in
test_panel_platform / test_panel_funnel / test_panel_sources_i18n pin that
side). Fixture mirrors test_panel_sources_i18n (CrawlSite + CrawlSource +
CrawlRun on the per-test sqlite, open_env so no token gate fires).
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.models import CrawlRun, CrawlSite, CrawlSource
from fd_open_data_mcp.panel import app as appmod
from fd_open_data_mcp.panel import i18n

GROUP_TEMPLATES = (
    "sources.html", "source_detail.html", "partial_sources_results.html",
    "_source_row.html", "funnel.html", "partial_funnel.html", "data.html",
)


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
    session.add(CrawlSource(source="unlit-src", site="demo",
                            schedule=None, enabled=True))
    session.add(CrawlRun(source="demo-src", status="success",
                         started_at=dt.datetime(2026, 10, 7, 5, 29, 9, 946),
                         rows_written=123))
    session.commit()


def test_every_t_key_in_group_templates_has_dictionary_entry():
    """A missed dictionary entry would leak the zh key onto en pages — catch
    it mechanically instead of per-page: every t('…') key used by the group
    templates must resolve to a non-zh value in the merged EN dictionary."""
    tpl_dir = Path(appmod.__file__).parent / "templates"
    key_re = re.compile(r"t\('([^']*)'\)")
    for name in GROUP_TEMPLATES:
        text = (tpl_dir / name).read_text(encoding="utf-8")
        keys = key_re.findall(text)
        assert keys, name  # the conversion must actually be there
        for key in keys:
            value = i18n.EN.get(key)
            assert value, f"{name}: {key!r} missing from EN"
            assert not re.search(r"[\u4e00-\u9fff]", value), \
                f"{name}: {key!r} -> {value!r} still carries zh"


def test_sources_board_renders_english_under_en_cookie(session, open_env):
    _seed(session)
    c = _client()
    html = c.get("/panel/sources", cookies={"panel-lang": "en"}).text
    # chips + row copy resolve from the group dictionary
    assert "not lit" in html                    # unlit chip + schedule cell
    assert ">Site<" in html and ">Schedule<" in html   # table headers
    assert ">ON<" in html                       # enabled badge
    assert ">trigger<" in html                  # row trigger button
    assert "daily 03:23" in html                # cron helper stays localized
    # and no zh leaks through the t() surface
    for zh in ("未点亮", "启用", "立即触发", "从未运行", "停滞"):
        assert zh not in html, zh


def test_sources_board_zh_unchanged(session, open_env):
    _seed(session)
    html = _client().get("/panel/sources").text
    assert "平台源" in html
    assert "未点亮 not lit" in html             # verbatim bilingual chrome
    assert "每天 03:23" in html
    assert 'title="23 3 * * *"' in html         # raw expression tooltip


def test_source_detail_renders_english_under_en_cookie(session, open_env):
    _seed(session)
    html = _client().get("/panel/sources/unlit-src",
                         cookies={"panel-lang": "en"}).text
    assert "not lit (no automatic runs)" in html
    assert ">Kind<" in html and ">Schedule<" in html
    assert "trigger now" in html
    assert "No pending runs." in html
    assert "No runs recorded yet." in html
    for zh in ("未点亮", "没有待运行务", "尚无运行记录", "立即触发"):
        assert zh not in html, zh


def test_funnel_and_data_pages_render_english_under_en_cookie(session,
                                                               open_env):
    _seed(session)
    c = _client()
    funnel = c.get("/panel/funnel", cookies={"panel-lang": "en"}).text
    assert "Discovery funnel" in funnel
    assert "this view is read-only" in funnel
    assert "loading…" in funnel
    assert "候选源漏斗" not in funnel and "加载中" not in funnel

    partial = c.get("/panel/partials/funnel",
                    cookies={"panel-lang": "en"}).text
    assert "Empty queue" in partial or "No approved manifests" in partial
    assert "队列为空" not in partial

    data = c.get("/panel/data", cookies={"panel-lang": "en"}).text
    assert "Data coverage" in data
    assert "Refresh coverage" in data
    assert "No observations match." in data
    assert "数据覆盖" not in data and "没有匹配的观测" not in data
