"""Policies group dictionary i18n smoke tests (panel-rbac-i18n-refresh 4.3).

Renders the policy board under the ``panel-lang=en`` cookie and asserts the
English dictionary surface (headers/buttons/badges/notices/estimate partial),
plus the zh default staying intact. Also guards that every ``t('…')`` key
used by the group's templates resolves in the merged dictionary.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy
from fd_open_data_mcp.panel import i18n
from fd_open_data_mcp.panel.app import app

client = TestClient(app)
HX = {"HX-Request": "true"}

TEMPLATES_DIR = Path(__file__).parent.parent / "fd_open_data_mcp" / "panel" / "templates"
GROUP_TEMPLATES = (
    "policies.html", "policy_edit.html", "estimate.html", "_policy_row.html",
)


@pytest.fixture
def open_env(monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID", "LOGTO_CLIENT_SECRET",
              "PANEL_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _policy(name="i18n-pol", enabled=True) -> int:
    """Same seed shape as tests/test_panel_policy_actions.py."""
    s = get_database().get_session()
    try:
        p = CrawlPolicy(name=name, enabled=enabled, concept_ids=[1],
                        entity_type="fund", cron_expr="0 6 * * *",
                        timezone="UTC", date_policy={"mode": "since_last"},
                        frequency="daily", mode="per_date")
        s.add(p)
        s.commit()
        return p.id
    finally:
        s.close()


def _en(path, **kwargs):
    return client.get(path, cookies={"panel-lang": "en"}, **kwargs)


def _norm(html):
    return re.sub(r"\s+", " ", html)


# ── dictionary hygiene ───────────────────────────────────────────────────────

def test_group_file_loaded_and_every_template_key_resolves():
    """i18n.py merged i18n_en_policies.EN_ENTRIES, and every ``t('…')`` key
    the group's templates use resolves to a translation (no silent zh
    fallback from a typo'd key). The brand key 柏讯·寻新 is exempt: it rides
    the zh fallback everywhere until 4.4 lands the Wire Scout caliber."""
    from fd_open_data_mcp.panel.i18n_en_policies import EN_ENTRIES

    assert EN_ENTRIES  # non-empty group file
    assert i18n.EN.get("从频率模板新建") == "New from frequency template"

    used: set[str] = set()
    for name in GROUP_TEMPLATES:
        used |= set(re.findall(r"\bt\('([^']+)'\)",
                               (TEMPLATES_DIR / name).read_text(encoding="utf-8")))
    assert used, "template key extraction found nothing — regex drifted"
    missing = sorted(k for k in used
                     if k not in i18n.EN and k != "柏讯·寻新")
    assert not missing, f"t() keys missing from the dictionary: {missing}"


def test_group_entries_are_wellformed():
    from fd_open_data_mcp.panel.i18n_en_policies import EN_ENTRIES
    for k, v in EN_ENTRIES.items():
        assert isinstance(k, str) and k.strip(), repr(k)
        assert isinstance(v, str), repr((k, v))


# ── en render smoke: list / editor / template / estimate / row fragment ──────

def test_policy_list_en(session, open_env):
    pid = _policy("list-en")
    r = _en("/panel/policies")
    assert r.status_code == 200
    t = _norm(r.text)
    # page name / template shortcut / search chrome / headers in English
    assert "New from frequency template" in t
    assert 'placeholder="search name/entity/id"' in r.text
    assert 'aria-label="Search policies"' in r.text
    assert "Search" in t and "Entity" in t and "Name" in t
    assert "Mode" in t and "Last run" in t
    # count envelope: "1 policies · Page 1/1"
    assert "1 policies · Page 1/1" in t
    # state badge + inline actions
    assert "Enable" in t and "Run now" in t
    assert 'title="bypasses cron, not guardrails"' in r.text
    assert "Delete" in t and "confirm" in t  # "保留" caliber owned by shell/auth
    # zh chrome must not bleed onto the en page
    assert "从频率模板新建" not in t and "立即运行" not in t


def test_empty_list_en(session, open_env):
    r = _en("/panel/policies")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "No policies match" in t and "Create one" in t
    assert "0 policies · Page 1/1" in t


def test_editor_new_and_template_en(session, open_env):
    editor = _norm(_en("/panel/policies/new").text)
    assert "New policy" in editor
    for label in ("Identity", "Entity type", "Entity IDs",
                  "Crawl scope", "Source filter", "Date policy",
                  "Trailing days", "Cron expr", "Timezone"):
        assert label in editor, label
    assert "(comma-separated; empty = all)" in editor
    assert "Save" in editor and "Cancel" in editor
    assert "Run once (no policy)" in editor
    assert 'title="Run this plan once without saving a policy — same fetch ceiling applies"' in editor
    assert "Estimate: —" in editor

    tmpl = _norm(_en("/panel/policies/template", params={"frequency": "daily"}).text)
    assert "Template policy: daily" in tmpl
    assert "Pre-selected the verified daily concepts and proposed the matching schedule below" in tmpl
    assert "adjust anything before saving; the result is an ordinary policy" in tmpl


def test_estimate_partial_en(session, open_env):
    r = client.post("/panel/estimate", cookies={"panel-lang": "en"}, data={
        "entity_type": "fund", "concept_ids": ["1"], "frequency": "daily",
        "mode": "per_date", "date_policy_mode": "since_last",
        "cron_expr": "0 6 * * *", "timezone": "UTC"})
    assert r.status_code == 200
    t = _norm(r.text)
    assert "Estimate:" in t and "fetches" in t
    assert "concepts" in t and "Mode" in t and "cap" in t
    assert "预估" not in t  # no zh bleed on the en fragment


def test_toggle_row_fragment_en(session, open_env):
    pid = _policy("toggle-en", enabled=True)
    r = client.post(f"/panel/policies/{pid}/toggle",
                    cookies={"panel-lang": "en"}, headers=HX)
    assert r.status_code == 200
    t = _norm(r.text)
    assert "Disable" in t and "Run now" in t and "Delete" in t
    assert "停用" not in t  # the flipped badge is fully en


# ── zh default unchanged ─────────────────────────────────────────────────────

def test_policy_list_zh_default(session, open_env):
    _policy("list-zh")
    r = client.get("/panel/policies")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "从频率模板新建" in t and "名称" in t and "实体" in t
    assert "上次运行" in t and "启用" in t and "立即运行" in t
    assert "确认删除" in t and "保留" in t
    # envelope bytes kept ("N 条 policies · 第 1/1 页" as before the change)
    assert "1 条 policies · 第 1/1 页" in t
    # no en bleed on the zh page (badge lost its ON/OFF half, headers their en)
    assert "New from frequency template" not in t
    assert "Last run" not in t and "Run now" not in t

    miss = _norm(client.get("/panel/policies", params={"q": "zzz"}).text)
    assert "没有匹配的策略" in miss and "清除" in miss


def test_editor_and_estimate_zh_default(session, open_env):
    editor = _norm(client.get("/panel/policies/new").text)
    assert "新建策略" in editor and "标识" in editor and "抓取范围" in editor
    assert "预估: —" in editor

    r = client.post("/panel/estimate", data={
        "entity_type": "fund", "concept_ids": ["1"], "frequency": "daily",
        "mode": "per_date", "date_policy_mode": "since_last",
        "cron_expr": "0 6 * * *", "timezone": "UTC"})
    assert "预估:" in r.text and "抓取" in r.text and "上限" in r.text
