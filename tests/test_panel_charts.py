"""Chart-shaping helpers + panel substrate tests (panel-ui-refresh).

Pure functions from fd_open_data_mcp.panel.charts plus the static assets and
shell the refreshed templates depend on (design D1–D6).
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from fd_open_data_mcp.panel.app import app
from fd_open_data_mcp.panel.charts import (
    bar_geometry, freshness_bucket, freshness_days, heatmap_tiles,
    progress_fraction, sparkline_geometry, timeline_buckets, timeline_geometry,
)

client = TestClient(app)


# ── bar chart geometry ────────────────────────────────────────────────────

def test_bar_geometry_scales_to_max():
    g = bar_geometry([0, 5, 10], labels=["a", "b", "c"])
    assert g["max"] == 10
    bars = g["bars"]
    assert bars[0]["bh"] == 0  # zero value renders no height
    assert bars[2]["bh"] > bars[1]["bh"] > 0
    assert bars[1]["title"] == "b: 5"


def test_bar_geometry_empty_and_all_zero_safe():
    assert bar_geometry([])["bars"] == []
    g = bar_geometry([0, 0])
    assert g["max"] == 1  # avoids division by zero
    assert all(b["bh"] == 0 for b in g["bars"])


# ── sparkline ─────────────────────────────────────────────────────────────

def test_sparkline_geometry_points_and_area():
    g = sparkline_geometry([1, 3, 2])
    assert len(g["points"].split()) == 3
    tokens = g["area"].split()
    assert tokens[0].endswith(f",{g['h'] - g['pad']}")  # closes to baseline
    assert tokens[-1].endswith(f",{g['h'] - g['pad']}")
    assert g["last"] is not None


def test_sparkline_geometry_empty():
    g = sparkline_geometry([])
    assert g["points"] == "" and g["area"] == "" and g["last"] is None


# ── progress fraction ─────────────────────────────────────────────────────

def test_progress_fraction_clamps():
    assert progress_fraction(None, 100) == 0.0
    assert progress_fraction(10, 0) == 0.0
    assert progress_fraction(25, 100) == 0.25
    assert progress_fraction(250, 100) == 1.0


# ── freshness buckets (spec boundaries: ≤7 fresh, 7–30 aging, >30 stale) ──

def test_freshness_bucket_boundaries():
    assert freshness_bucket(None) == "never"
    assert freshness_bucket(0) == "fresh"
    assert freshness_bucket(7) == "fresh"
    assert freshness_bucket(7.1) == "aging"
    assert freshness_bucket(30) == "aging"
    assert freshness_bucket(31) == "stale"


def test_freshness_days_from_iso_string():
    today = dt.date(2026, 9, 10)
    assert freshness_days(None, today) is None
    assert freshness_days("2026-09-10", today) == 0
    assert freshness_days("2026-08-05", today) == 36


# ── fetch timeline ────────────────────────────────────────────────────────

def test_timeline_buckets_splits_by_time():
    t0 = dt.datetime(2026, 9, 10, 12, 0)
    samples = [(t0, True), (t0 + dt.timedelta(minutes=50), False),
               (t0 + dt.timedelta(minutes=100), True)]
    buckets = timeline_buckets(samples, n=4)
    assert sum(b["ok"] for b in buckets) == 2
    assert sum(b["err"] for b in buckets) == 1
    assert timeline_buckets([], n=4) == [{"i": i, "ok": 0, "err": 0} for i in range(4)]


def test_timeline_geometry_stacks_err_on_ok():
    g = timeline_geometry([{"i": 0, "ok": 2, "err": 1}, {"i": 1, "ok": 0, "err": 0}])
    col = g["cols"][0]
    assert col["err_y"] < col["ok_y"]  # error sits on top of ok
    assert "ok 2, err 1" in col["title"]


# ── heatmap tiles ─────────────────────────────────────────────────────────

def test_heatmap_tiles_covers_all_buckets():
    g = heatmap_tiles({"fresh": 1, "aging": 1, "stale": 1, "never": 1})
    assert [t["key"] for t in g["tiles"]] == ["fresh", "aging", "stale", "never"]
    assert all(t["count"] == 1 for t in g["tiles"])
    missing = heatmap_tiles({})
    assert all(t["count"] == 0 for t in missing["tiles"])


# ── substrate: static assets + shell (tasks 1.1–1.5) ─────────────────────

def test_static_assets_served():
    for path in ("/panel/static/app.css", "/panel/static/app.js",
                 "/panel/static/htmx.min.js"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_home_has_shell_theme_and_toast_hooks(session):
    r = client.get("/panel")
    assert r.status_code == 200
    text = r.text
    # sidebar shell + bilingual nav (design D6, spec bilingual labels)
    for marker in ('class="shell"', 'class="sidenav"', "总览", "策略", "爬虫控制台"):
        assert marker in text, marker
    # theme: pre-paint script + manual toggle (spec theme requirement)
    assert 'localStorage.getItem("panel-theme")' in text
    assert 'id="theme-toggle"' in text
    # toast region for inline-action feedback (design D5)
    assert 'id="toast-region"' in text
    # tokens stylesheet + no external origins (spec no-build requirement)
    assert 'href="/panel/static/app.css"' in text
    assert "http://" not in text.replace("http://www.w3.org", "")  # svg ns only
