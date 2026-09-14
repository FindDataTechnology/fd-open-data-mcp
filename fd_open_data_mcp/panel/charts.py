"""Pure geometry/bucketing helpers for panel SVG charts (panel-ui-refresh D4).

Input shaping only — no DB, no Jinja. Everything here is unit-tested in
tests/test_panel_charts.py; the ``_macros.html`` SVG macros stay dumb
renderers of the geometry these functions produce.
"""
from __future__ import annotations

import datetime as dt

# Freshness buckets for the coverage heatmap (panel-ui-refresh spec):
# fresh ≤ 7 days, aging 7–30 days, stale > 30 days, never observed.
FRESHNESS_BUCKETS: tuple[tuple[str, str], ...] = (
    ("fresh", "新鲜 ≤7d"),
    ("aging", "老化 7–30d"),
    ("stale", "过期 >30d"),
    ("never", "无观测 never"),
)


def bar_geometry(values: list[float], labels: list[str] | None = None,
                 w: int = 560, h: int = 150, pad: int = 30, gap: float = 0.3) -> dict:
    """Column-chart geometry for non-negative daily values."""
    n = len(values)
    mx = max(values, default=0) or 1  # all-zero series still renders a baseline
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    slot = inner_w / n if n else inner_w
    bw = slot * (1 - gap)
    bars = []
    for i, v in enumerate(values):
        bh = inner_h * (v / mx)
        x = pad + i * slot + (slot - bw) / 2
        label = labels[i] if labels and i < len(labels) else str(i)
        bars.append({"x": round(x, 1), "y": round(h - pad - bh, 1),
                     "bw": round(bw, 1), "bh": round(bh, 1),
                     "label": label, "title": f"{label}: {int(v)}", "value": v})
    return {"bars": bars, "w": w, "h": h, "pad": pad, "max": mx}


def sparkline_geometry(values: list[float], w: int = 200, h: int = 48,
                       pad: int = 4) -> dict:
    """Line geometry: `points` is an SVG polyline points string."""
    n = len(values)
    if n == 0:
        return {"points": "", "area": "", "last": None, "w": w, "h": h}
    mx, mn = max(values), min(values)
    span = (mx - mn) or 1.0
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0.0
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = pad + (h - 2 * pad) * (1 - (v - mn) / span)
        pts.append((round(x, 1), round(y, 1)))
    points = " ".join(f"{x},{y}" for x, y in pts)
    area = f"{pts[0][0]},{h - pad} " + points + f" {pts[-1][0]},{h - pad}"
    return {"points": points, "area": area, "last": pts[-1], "w": w, "h": h,
            "pad": pad}


def progress_fraction(value: float | None, maximum: float | None) -> float:
    """Clamped fill fraction for the running-run progress bar."""
    if not value or not maximum:
        return 0.0
    return min(1.0, max(0.0, value / maximum))


def freshness_bucket(days: float | None) -> str:
    """Bucket key for days since a concept's latest observation."""
    if days is None:
        return "never"
    if days <= 7:
        return "fresh"
    if days <= 30:
        return "aging"
    return "stale"


def freshness_days(latest_date, today: dt.date | None = None) -> float | None:
    """Days since `latest_date` (a date / ISO string); None when never observed."""
    if latest_date is None:
        return None
    if isinstance(latest_date, str):
        latest_date = dt.date.fromisoformat(latest_date[:10])
    if isinstance(latest_date, dt.datetime):
        latest_date = latest_date.date()
    today = today or dt.date.today()
    return (today - latest_date).days


def timeline_buckets(samples: list[tuple[dt.datetime, bool]], n: int = 12) -> list[dict]:
    """Bucket (timestamp, is_ok) fetch samples into n equal time buckets."""
    buckets = [{"i": i, "ok": 0, "err": 0} for i in range(n)]
    if not samples:
        return buckets
    t0 = min(t for t, _ in samples)
    t1 = max(t for t, _ in samples)
    span = (t1 - t0).total_seconds() or 1.0
    for t, is_ok in samples:
        idx = min(n - 1, int((t - t0).total_seconds() / span * (n - 1)))
        buckets[idx]["ok" if is_ok else "err"] += 1
    return buckets


def timeline_geometry(buckets: list[dict], w: int = 560, h: int = 96,
                      pad: int = 18) -> dict:
    """Stacked ok/err column geometry; ok on the bottom, errors on top."""
    n = max(len(buckets), 1)
    mx = max((b["ok"] + b["err"]) for b in buckets) or 1
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    slot = inner_w / n
    bw = slot * 0.66
    cols = []
    for b in buckets:
        total = b["ok"] + b["err"]
        x = pad + b["i"] * slot + (slot - bw) / 2
        ok_h = inner_h * (b["ok"] / mx)
        err_h = inner_h * (b["err"] / mx)
        cols.append({
            "i": b["i"], "x": round(x, 1), "bw": round(bw, 1),
            "ok_y": round(h - pad - ok_h, 1), "ok_h": round(ok_h, 1),
            "err_y": round(h - pad - ok_h - err_h, 1), "err_h": round(err_h, 1),
            "title": f"bucket {b['i'] + 1}/{n}: ok {b['ok']}, err {b['err']}",
            "total": total})
    return {"cols": cols, "w": w, "h": h, "pad": pad, "max": mx}


def heatmap_tiles(counts: dict[str, int], w: int = 460, h: int = 120) -> dict:
    """Four equal tiles (one per freshness bucket) with counts and thresholds."""
    gap = 10
    tw = (w - 3 * gap) / 4
    tiles = []
    thresholds = {"fresh": "≤ 7d", "aging": "7–30d", "stale": "> 30d", "never": "—"}
    for i, (key, label) in enumerate(FRESHNESS_BUCKETS):
        x = i * (tw + gap)
        count = counts.get(key, 0)
        tiles.append({"key": key, "x": round(x, 1), "w": round(tw, 1),
                      "cx": round(x + tw / 2, 1),
                      "label": label, "threshold": thresholds[key],
                      "count": count,
                      "title": f"{label} ({thresholds[key]}): {count}"})
    return {"tiles": tiles, "w": w, "h": h}
