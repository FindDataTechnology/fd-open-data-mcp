"""Panel routes: observability home + partials, run detail, data coverage,
policy list + toggles, editor with estimate preview, runs view, proxy ops.

Served standalone (``uvicorn fd_open_data_mcp.panel.app:app`` / CLI ``panel``)
or mounted under /panel via ``mcp.http_app().mount``. All routes hit the same
``crawl_policies``/``policy_runs`` tables as the MCP tools.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from fd_open_data_mcp import station_ops
from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    BanRule, Cluster, Concept, CrawlLoginStation, CrawlPolicy, CrawlRun,
    CrawlSite, CrawlSource, FetchLog, PendingRun, PolicyRun, Proxy,
    SourceProxyHealth, SourceRateLimit,
)
from fd_open_data_mcp.platform_tools import (
    cancel_pending_run, cancel_platform_run, trigger_platform_run,
)
from fd_open_data_mcp.visibility import snapshot as _snapshot

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
logger = logging.getLogger(__name__)

FREQUENCIES = ["daily", "weekly", "monthly", "quarterly", "yearly"]
MODES = ["series", "per_date"]
DATE_POLICY_MODES = ["since_last", "trailing", "explicit"]

# htmx poll cadence for the home partials (design D2) and the reconciler
# liveness banner threshold (design: a suspended scheduler must be visible).
POLL_SECONDS = 15
RECONCILER_QUIET_HOURS = 24
# census rows older than this show a staleness marker (add-shard-aware-coverage)
CENSUS_STALE_HOURS = 24

# panel-ops-console 6.x: frequency-template proposals. The cron proposals and
# the daily trailing-1d date policy are input aids only — the saved artifact is
# an ordinary policy row (design D7).
_TEMPLATE_CRON = {
    "daily": "0 6 * * *", "weekly": "0 6 * * 1", "monthly": "0 6 1 * *",
    "quarterly": "0 6 1 1,4,7,10 *", "yearly": "0 6 1 1 *",
}


def _template_concepts(s, frequency: str) -> list[Concept]:
    """Hygiene-filtered concept pre-selection for a frequency template:
    enabled concept cadence = the concept's own frequency; deprecated and
    unverified concepts are excluded (spec crawl-control-center)."""
    return (s.query(Concept)
            .filter_by(frequency=frequency, deprecated=False, verified=True)
            .order_by(Concept.code).all())


def _session():
    return get_database().get_session()


def _policy_or_404(s, pid: int) -> CrawlPolicy:
    p = s.query(CrawlPolicy).get(pid)
    if not p:
        raise HTTPException(404, f"policy {pid} not found")
    return p


def _entity_types(s) -> list[str]:
    rows = s.query(CrawlPolicy.entity_type).distinct().all()
    return [r[0] for r in rows]


def _run_estimate(s, plan_json: dict | None) -> int:
    from fd_open_data_mcp.crawl.plan import CrawlPlan
    from fd_open_data_mcp.refresh.reconciler import estimate_fetches

    if not plan_json:
        return 0
    try:
        return estimate_fetches(s, CrawlPlan.model_validate(plan_json))
    except Exception:  # noqa: BLE001
        return 0


def _concept_groups(s) -> list[tuple[str, list[Concept]]]:
    """Concepts grouped by category for the editor multi-select."""
    groups: dict[str, list[Concept]] = {}
    for c in s.query(Concept).filter_by(deprecated=False).order_by(Concept.code).all():
        cat = c.category or (c.code.split(".")[0] if "." in c.code else c.code)
        groups.setdefault(cat, []).append(c)
    return sorted(groups.items())


def _parse_concept_ids(form_concepts: list[str]) -> list[int]:
    return [int(x) for x in form_concepts if x]


def _parse_entity_ids(raw: str) -> list[int] | None:
    ids = [int(x) for x in raw.replace(" ", "").split(",") if x]
    return ids or None  # empty -> all entities


def _parse_source_filter(raw: str) -> list[str] | None:
    vals = [x.strip() for x in raw.split(",") if x.strip()]
    return vals or None


def _parse_date_policy(
    mode: str, days: str, start: str, end: str,
) -> dict:
    if mode == "trailing":
        return {"mode": "trailing", "days": int(days) if days else 1}
    if mode == "explicit":
        return {"mode": "explicit", "start": start or None, "end": end or None}
    return {"mode": "since_last"}


# ── proxy operations (panel-ops-console) ─────────────────────────────────────
def _mask_auth(auth: str | None) -> str:
    """Mask a proxy credential for ANY panel rendering — the full value must
    never appear in a panel response body (spec crawl-control-center)."""
    if not auth:
        return "—"
    head = auth.split(":", 1)[0][:2]
    return f"{head}•••••"


def _proxy_control_ready() -> bool:
    """Management actions need the proxy-control API; without it the proxy
    pages degrade to read-only (design D1, ships-dark philosophy)."""
    return bool(os.environ.get("PROXY_CONTROL_URL"))


def _proxy_control(method: str, path: str, payload: dict | None = None) -> dict:
    """POST/PUT a management action to the proxy-control API.

    Raises RuntimeError on any transport/API failure; callers turn that into a
    redirect-with-error rather than a 500."""
    base = os.environ.get("PROXY_CONTROL_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("PROXY_CONTROL_URL is not configured")
    body = json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    mgmt_token = os.environ.get("PROXY_CONTROL_TOKEN")
    if mgmt_token:
        headers["X-Management-Token"] = mgmt_token
    req = urllib.request.Request(f"{base}{path}", data=body, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001 - normalized for the panel UX
        raise RuntimeError(f"proxy-control {method} {path} failed: {e}") from e


def _proxy_redirect(msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import quote
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/panel/proxy{q}", status_code=303)


def _run_launcher():
    """The reconciler's default launcher, resolved lazily (run cancel needs
    its delete path; module-level so tests can stub it)."""
    from fd_open_data_mcp.refresh.reconciler import _default_launcher
    return _default_launcher()


def _station_client():
    """The station k8s client seam (module-level so tests stub it with a fake
    — no station pod is ever really launched from tests)."""
    return station_ops.get_station_client()


def _station_upstream(station_id: int) -> str | None:
    """``host:port`` of a live station's noVNC endpoint, or None when the
    station is unknown / not in flight / its Service is not resolvable.
    Applies the deadline-timeout backstop on read; module-level seam so tests
    can point the proxy and the websocket relay at a local upstream."""
    s = _session()
    try:
        station_ops.active_stations_summary(s)  # timeout backstop on read
        st = s.get(CrawlLoginStation, station_id)
        if st is None or st.status not in station_ops.STATION_OPEN:
            return None
        client = _station_client()
        names = client.list_names("Service", f"station-id={station_id}")
        if not names:
            return None
        return (f"{names[0]}.{client.namespace}.svc.cluster.local:"
                f"{station_ops.STATION_PORT}")
    except Exception:  # noqa: BLE001 - no resolvable station -> friendly error
        return None
    finally:
        s.close()


def _station_unavailable(station_id: int) -> str:
    """Friendly (non-500) page for a station the proxy cannot serve."""
    return (
        "<h1>登录站不可用 login station unavailable</h1>"
        f'<p>站 #{station_id} 不存在、已完成或已回收，观察通道已关闭。'
        f"Station #{station_id} does not exist, already finished or was "
        f"reclaimed; the observation channel is closed.</p>")


def _station_unreachable(station_id: int, err: Exception) -> str:
    """Friendly 502 page: the station exists but its noVNC endpoint did not
    answer (typically still booting)."""
    return (
        "<h1>登录站暂不可达 login station unreachable</h1>"
        f'<p>站 #{station_id} 可能仍在拉起（约需数十秒），请稍后刷新重试；'
        f"持续失败时请回收后重新拉起。Station #{station_id} is probably still "
        f"booting — retry shortly; if it keeps failing, reclaim and relaunch."
        f"</p><p class='muted'>{type(err).__name__}</p>")


# ── cockpit aggregations (panel-ui-refresh: KPI row + yield trend) ─────────
def kpi_snapshot(s) -> dict:
    """Home KPI row from `policy_runs`: running count, 24h terminal-run
    success rate, 24h summed new rows, and 24h failures (failed +
    zero_yield — the two operator-actionable terminal states)."""
    now = dt.datetime.utcnow()
    since = now - dt.timedelta(hours=24)
    running = int(s.query(func.count(PolicyRun.id))
                  .filter(PolicyRun.status == "running").scalar() or 0)
    by_status: dict[str, int] = dict(
        s.query(PolicyRun.status, func.count(PolicyRun.id))
        .filter(PolicyRun.finished_at >= since,
                PolicyRun.status != "running")
        .group_by(PolicyRun.status).all())
    terminal = sum(by_status.values())
    successes = by_status.get("success", 0)
    new_rows = int(s.query(func.coalesce(func.sum(PolicyRun.rows_new), 0))
                   .filter(PolicyRun.finished_at >= since,
                           PolicyRun.rows_new.isnot(None)).scalar() or 0)
    return {
        "running": running,
        "success_rate": round(100 * successes / terminal) if terminal else None,
        "new_rows": new_rows,
        "failures": by_status.get("failed", 0) + by_status.get("zero_yield", 0),
    }


def yield_trend(s, days: int = 14) -> dict:
    """Daily summed rows_new over the window, zero-filled, as bar geometry
    (panel.charts) so the home template renders it directly."""
    from fd_open_data_mcp.panel.charts import bar_geometry

    now = dt.datetime.utcnow()
    since = now - dt.timedelta(days=days - 1)
    # func.date() returns a string on sqlite but a date on postgres —
    # normalize keys to ISO strings so the zero-fill lookup always matches
    rows = {
        (k.isoformat() if hasattr(k, "isoformat") else str(k)): int(v or 0)
        for k, v in s.query(func.date(PolicyRun.finished_at),
                            func.sum(PolicyRun.rows_new))
        .filter(PolicyRun.finished_at >= since,
                PolicyRun.rows_new.isnot(None))
        .group_by(func.date(PolicyRun.finished_at)).all()
    }
    labels, values = [], []
    for i in range(days):
        day = (since + dt.timedelta(days=i)).date()
        labels.append(day.strftime("%m-%d"))
        values.append(rows.get(day.isoformat(), 0))
    return bar_geometry(values, labels=labels)


def _policy_from_form(form) -> dict:
    """Extract a policy payload dict from a submitted form."""
    return {
        "name": form.get("name", "").strip(),
        "enabled": "enabled" in form,
        "concept_ids": _parse_concept_ids(form.getlist("concept_ids")),
        "entity_type": form["entity_type"],
        "entity_ids": _parse_entity_ids(form.get("entity_ids", "")),
        "date_policy": _parse_date_policy(
            form.get("date_policy_mode", "since_last"),
            form.get("date_policy_days", ""), form.get("date_policy_start", ""),
            form.get("date_policy_end", "")),
        "frequency": form.get("frequency", "daily"),
        "mode": form.get("mode", "per_date"),
        "source_filter": _parse_source_filter(form.get("source_filter", "")),
        "force": "force" in form,
        "cron_expr": form.get("cron_expr", "0 6 * * *"),
        "timezone": form.get("timezone", "UTC"),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Crawl Control Center")
    app.mount("/panel/static", StaticFiles(directory=str(HERE / "static")),
              name="panel_static")

    # ── auth gate (panel-logto-auth, design D3) ────────────────────────────
    # Precedence: PANEL_TOKEN (programmatic) → OIDC session cookie → redirect
    # to Logto login (when LOGTO_* configured) → legacy 401. The OIDC routes
    # themselves and static assets are always public; every OTHER /panel/auth
    # path — the login-station observation channel and the identity writes
    # (login-station-console 3.2: 观察通道不裸奔) — goes through this same gate.
    token = os.environ.get("PANEL_TOKEN")
    from fd_open_data_mcp.panel import auth as _auth

    _PUBLIC_AUTH_PATHS = ("/panel/auth/login", "/panel/auth/callback",
                          "/panel/auth/logout", "/panel/auth/whoami")

    @app.middleware("http")
    async def gate(request: Request, call_next):
        cfg = _auth.logto_config()
        path = request.url.path
        if path in _PUBLIC_AUTH_PATHS or path.startswith("/panel/static"):
            return await call_next(request)
        if token:
            q = request.query_params.get("token")
            if q == token:
                resp = await call_next(request)
                resp.set_cookie("panel_token", token)
                return resp
            if (request.headers.get("X-Panel-Token") == token
                    or request.cookies.get("panel_token") == token):
                return await call_next(request)
        session = _auth.read_session(request.cookies.get(_auth.SESSION_COOKIE))
        if session is not None:
            # Role gate takes precedence (panel-role-gate D2): re-check the
            # roles frozen into the session at login — removals in Logto
            # propagate at next login, not mid-session.
            role = _auth.required_role()
            if role is not None:
                if role in session["roles"]:
                    request.state.panel_user = session
                    return await call_next(request)
                return HTMLResponse(
                    f"<h1>403 - missing required role {role}</h1>",
                    status_code=403)
            allowed = _auth.allow_list()
            if allowed is None or session["sub"] in allowed:
                request.state.panel_user = session
                return await call_next(request)
            return HTMLResponse("<h1>403 - user not in PANEL_USER_IDS</h1>",
                                status_code=403)
        if cfg is not None:
            return RedirectResponse("/panel/auth/login", status_code=302)
        if token is not None:
            return HTMLResponse("<h1>401 - set PANEL_TOKEN / ?token=</h1>",
                                status_code=401)
        return await call_next(request)  # no gate configured — open panel

    # ── OIDC routes (panel-logto-auth) ─────────────────────────────────────
    @app.get("/panel/auth/login")
    def auth_login():
        cfg = _auth.logto_config()
        if cfg is None:
            return HTMLResponse("<h1>401 - Logto not configured</h1>", status_code=401)
        state, cookie_value = _auth.make_state()
        resp = RedirectResponse(_auth.authorize_url(cfg, state), status_code=302)
        resp.set_cookie(_auth.STATE_COOKIE, cookie_value, httponly=True,
                        samesite="lax", max_age=600)
        return resp

    @app.get("/panel/auth/callback")
    def auth_callback(request: Request, code: str = "", state: str = "",
                      error: str = "", error_description: str = ""):
        cfg = _auth.logto_config()
        if cfg is None:
            return HTMLResponse("<h1>401 - Logto not configured</h1>", status_code=401)
        if error:
            return HTMLResponse(
                f"<h1>login failed</h1><p>{error}: {error_description}</p>",
                status_code=401)
        if not _auth.check_state(request.cookies.get(_auth.STATE_COOKIE), state):
            return HTMLResponse("<h1>401 - bad state</h1>", status_code=401)
        try:
            token_response = _auth.exchange_code(cfg, code)
            claims = _auth.id_token_claims(cfg, token_response)
        except Exception as e:  # noqa: BLE001 - provider/network errors
            return HTMLResponse(f"<h1>login failed</h1><p>{e}</p>", status_code=401)
        role = _auth.required_role()
        roles: list[str] = []
        if role is not None:
            # Role admission (panel-role-gate): no roles claim anywhere or
            # role not held → 403 before any session cookie is issued.
            extracted = _auth.extract_roles(cfg, claims, token_response)
            if extracted is None:
                return HTMLResponse("<h1>403 - no roles claim in token</h1>",
                                    status_code=403)
            if role not in extracted:
                return HTMLResponse(
                    f"<h1>403 - missing required role {role}</h1>",
                    status_code=403)
            roles = extracted
        else:
            allowed = _auth.allow_list()
            if allowed is not None and claims.get("sub") not in allowed:
                return HTMLResponse("<h1>403 - user not in PANEL_USER_IDS</h1>",
                                    status_code=403)
        resp = RedirectResponse("/panel", status_code=302)
        resp.set_cookie(_auth.SESSION_COOKIE,
                        _auth.make_session_value(claims.get("sub", ""),
                                                 claims.get("name", ""), roles),
                        httponly=True, samesite="lax",
                        max_age=_auth.SESSION_HOURS * 3600)
        resp.delete_cookie(_auth.STATE_COOKIE)
        return resp

    @app.get("/panel/auth/logout")
    def auth_logout():
        resp = RedirectResponse("/panel", status_code=302)
        resp.delete_cookie(_auth.SESSION_COOKIE)
        return resp

    @app.get("/panel/auth/whoami", response_class=HTMLResponse)
    def auth_whoami(request: Request):
        session = getattr(request.state, "panel_user", None) or _auth.read_session(
            request.cookies.get(_auth.SESSION_COOKIE))
        if session is None:
            return HTMLResponse("")
        return HTMLResponse(
            f'<span>{session["name"]}</span> '
            f'<a href="/panel/auth/logout" class="btn">退出 logout</a>')

    # ── pages ──────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse("/panel")

    def _unavailable(e: Exception) -> HTMLResponse:
        # One failing partial must not fail the page (spec: live panel refresh)
        return HTMLResponse(f'<p class="muted">分区暂不可用 section unavailable: {e}</p>')

    def _scheduler_quiet(s) -> tuple[bool, str | None]:
        """True when no run has started within RECONCILER_QUIET_HOURS — the
        suspended-reconciler case stays visible instead of silent."""
        last = s.query(func.max(PolicyRun.started_at)).scalar()
        if last is None:
            return True, None
        # TIMESTAMP WITHOUT TIME ZONE column, naive-UTC per the writer contract
        age_h = (dt.datetime.utcnow() - last).total_seconds() / 3600
        return age_h > RECONCILER_QUIET_HOURS, last.isoformat()

    @app.get("/panel", response_class=HTMLResponse)
    def home(request: Request):
        s = _session()
        try:
            quiet, last_started = _scheduler_quiet(s)
            return templates.TemplateResponse(
                request, "home.html",
                {"poll_seconds": POLL_SECONDS,
                 "quiet_hours": RECONCILER_QUIET_HOURS,
                 "scheduler_quiet": quiet, "last_run_started": last_started,
                 "kpi": kpi_snapshot(s), "trend": yield_trend(s)})
        finally:
            s.close()

    # ── home partials (htmx polling; each degrades independently) ──────────
    @app.get("/panel/partials/running", response_class=HTMLResponse)
    def partial_running(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.running_runs(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_running.html", {"runs": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/recent", response_class=HTMLResponse)
    def partial_recent(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.recent_runs(s, limit=15)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_recent.html", {"runs": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/fleet", response_class=HTMLResponse)
    def partial_fleet(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.fleet_health(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_fleet.html", {"fleet": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/next", response_class=HTMLResponse)
    def partial_next(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.next_runs(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_next.html", {"upcoming": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/missed", response_class=HTMLResponse)
    def partial_missed(request: Request):
        """Missed-run board (panel-ops-console): enabled policies whose latest
        expected fire passed the grace interval with no successful run."""
        try:
            s = _session()
            try:
                rows = _snapshot.missed_runs(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_missed.html", {"missed": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    # ── run detail ─────────────────────────────────────────────────────────
    @app.get("/panel/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        s = _session()
        try:
            row = (
                s.query(PolicyRun, CrawlPolicy.name, Cluster.name)
                .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
                .outerjoin(Cluster, PolicyRun.cluster_id == Cluster.id)
                .filter(PolicyRun.id == run_id)
                .first()
            )
            if not row:
                raise HTTPException(404, f"run {run_id} not found")
            run, policy_name, cluster_name = row

            plan = run.plan_json if isinstance(run.plan_json, dict) else {}
            concepts = [
                {"id": pc.get("concept_id"), "code": pc.get("code"),
                 "sources": [rs.get("source") for rs in pc.get("ranked_sources") or []]}
                for pc in plan.get("wanted_concepts") or []
            ]
            scope = plan.get("entity_scope") or {}
            date_range = plan.get("date_range") or {}

            # fetch_log carries no run key (design D5): approximate by the run's
            # window filtered to its plan concepts + cluster, labeled as such.
            from collections import Counter

            from fd_open_data_mcp.panel.charts import (
                timeline_buckets, timeline_geometry)

            fetch_summary: list[dict] = []
            fetch_timeline = None
            window_end = run.finished_at or dt.datetime.utcnow()
            if run.started_at:
                concept_ids = [c["id"] for c in concepts if c["id"] is not None]
                fq = (
                    s.query(FetchLog.timestamp, FetchLog.status)
                    .filter(FetchLog.timestamp >= run.started_at,
                            FetchLog.timestamp <= window_end,
                            FetchLog.cluster_id == run.cluster_id)
                )
                if concept_ids:
                    fq = fq.filter(FetchLog.concept_id.in_(concept_ids))
                rows = fq.all()
                samples = [(t, status == "ok") for t, status in rows]
                fetch_summary = [
                    {"status": status, "count": int(cnt)}
                    for status, cnt in Counter(st for _, st in rows).items()
                ]
                fetch_timeline = timeline_geometry(timeline_buckets(samples))
            return templates.TemplateResponse(
                request, "run_detail.html",
                {"run": run.toDict(), "policy_name": policy_name,
                 "cluster_name": cluster_name,
                 "plan": {"mode": plan.get("mode"),
                          "concepts": concepts,
                          "entity_type": scope.get("entity_type"),
                          "entity_ids": scope.get("entity_ids"),
                          "start": date_range.get("start"),
                          "end": date_range.get("end")},
                 "fetch_summary": fetch_summary,
                 "fetch_timeline": fetch_timeline,
                 "fetch_window": [run.started_at.isoformat() if run.started_at else None,
                                  run.finished_at.isoformat() if run.finished_at else "now"],
                 "duration_min": (int(((run.finished_at or dt.datetime.utcnow())
                                       - run.started_at).total_seconds() // 60)
                                 if run.started_at else None)})
        finally:
            s.close()

    # ── data coverage ──────────────────────────────────────────────────────
    def _census_rows(s) -> list[dict]:
        """Stored census rows + staleness flags; never collects (design D3)."""
        from fd_open_data_mcp.visibility.census import latest_census

        out = []
        now = dt.datetime.utcnow()
        for r in latest_census(s):
            sampled = r.get("sampled_at")
            age_h = None
            if sampled:
                t = dt.datetime.fromisoformat(sampled)
                if t.tzinfo is not None:
                    t = t.replace(tzinfo=None)
                age_h = (now - t).total_seconds() / 3600
            r["age_hours"] = round(age_h, 1) if age_h is not None else None
            r["stale"] = age_h is None or age_h > CENSUS_STALE_HOURS
            out.append(r)
        return out

    @app.get("/panel/data", response_class=HTMLResponse)
    def data_coverage(request: Request, concept_id: str = "",
                      entity_type: str = "", freshness: str = ""):
        try:
            cid: int | None = int(concept_id) if concept_id else None
        except ValueError:
            cid = None
        from fd_open_data_mcp.panel.charts import (
            freshness_bucket, freshness_days, heatmap_tiles)
        from fd_open_data_mcp.visibility.coverage import coverage_by_concept

        s = _session()
        try:
            # heatmap counts come from the unfiltered universe; never-observed
            # concepts appear in no observation row, so count them from Concept
            all_rows = coverage_by_concept(s)
            universe = (s.query(Concept)
                        .filter_by(deprecated=False).count())
            counts: dict[str, int] = {}
            for r in all_rows:
                key = freshness_bucket(freshness_days(r["latest_date"]))
                counts[key] = counts.get(key, 0) + 1
            counts["never"] = max(
                0, universe - len({r["concept_id"] for r in all_rows}))
            heat = heatmap_tiles(counts)

            rows = (coverage_by_concept(s, concept_id=cid,
                                        entity_type=entity_type or None)
                    if cid or entity_type else all_rows)
            if freshness:
                rows = [r for r in rows
                        if freshness_bucket(freshness_days(r["latest_date"])) == freshness]
            total_rows = sum(r["rows"] for r in rows)
            census_rows = _census_rows(s)
            return templates.TemplateResponse(
                request, "data.html",
                {"coverage": rows, "total_rows": total_rows,
                 "n_concepts": len(rows),
                 "census": census_rows,
                 "census_total": sum(r.get("approx_rows") or 0 for r in census_rows),
                 "concept_id": cid, "entity_type": entity_type,
                 "freshness": freshness, "heat": heat})
        finally:
            s.close()

    @app.post("/panel/data/census/refresh")
    def data_census_refresh():
        from fd_open_data_mcp.visibility.census import refresh_census

        s = _session()
        try:
            refresh_census(s)
        finally:
            s.close()
        return RedirectResponse("/panel/data", status_code=303)

    @app.get("/panel/policies", response_class=HTMLResponse)
    def policy_list(request: Request):
        s = _session()
        try:
            policies = s.query(CrawlPolicy).order_by(CrawlPolicy.id).all()
            return templates.TemplateResponse(
                request, "policies.html",
                {"policies": [p.toDict() for p in policies], "entity_types": _entity_types(s)})
        finally:
            s.close()

    def _run_row_dict(s, r: PolicyRun, policies: dict) -> dict:
        d = {"id": r.id, "policy": policies.get(r.policy_id, f"#{r.policy_id}"),
             "policy_id": r.policy_id, "origin": r.origin,
             "status": r.status, "job_ref": r.job_ref,
             "started_at": r.started_at.isoformat() if r.started_at else None,
             "finished_at": r.finished_at.isoformat() if r.finished_at else None,
             "detail": r.detail}
        d["estimate"] = _run_estimate(s, r.plan_json)
        return d

    def _toast(resp, message: str, level: str = "ok"):
        # Inline actions announce outcomes via HX-Trigger; app.js renders the toast.
        resp.headers["HX-Trigger"] = json.dumps(
            {"toast": {"message": message, "level": level}})
        return resp

    def _run_row_response(request: Request, s, run_id: int):
        r = s.query(PolicyRun).get(run_id)
        if r is None:
            return None
        policies = {p.id: p.name for p in s.query(CrawlPolicy).all()}
        return templates.TemplateResponse(
            request, "_run_row.html",
            {"r": _run_row_dict(s, r, policies), "policies": policies})

    @app.get("/panel/runs", response_class=HTMLResponse)
    def runs(request: Request, status: str = "", policy_id: str = ""):
        # `policy_id` arrives as a string because the filter form/chips submit
        # the select even when empty ("all policies"); "" means no filter
        try:
            pid: int | None = int(policy_id) if policy_id else None
        except ValueError:
            pid = None
        s = _session()
        try:
            q = s.query(PolicyRun)
            if status:
                q = q.filter_by(status=status)
            if pid is not None:
                q = q.filter_by(policy_id=pid)
            runs_rows = q.order_by(PolicyRun.started_at.desc()).limit(200).all()
            policies = {p.id: p.name for p in s.query(CrawlPolicy).all()}
            rendered = [_run_row_dict(s, r, policies) for r in runs_rows]
            # crawl-platform: platform runs (crawl_runs) shown side by side
            # with policy_runs — the policy columns above stay untouched.
            platform = _snapshot.platform_runs(s, limit=50)
            ctx = {"runs": rendered, "status": status, "policies": policies,
                   "platform_runs": platform}
            # status chips / policy filter swap just the results region
            if request.headers.get("hx-request") == "true":
                return templates.TemplateResponse(
                    request, "partial_runs_results.html", ctx)
            return templates.TemplateResponse(request, "runs.html", ctx)
        finally:
            s.close()

    # ── run control (panel-ops-console) ─────────────────────────────────────
    @app.post("/panel/runs/{run_id}/cancel")
    def run_cancel(run_id: int, request: Request):
        """Cancel an open run: CAS row close + best-effort Job deletion.
        HTMX requests get the re-rendered row + toast; plain posts redirect."""
        from fd_open_data_mcp.refresh.runs import cancel_run

        hx = request.headers.get("hx-request") == "true"
        s = _session()
        try:
            try:
                out = cancel_run(s, run_id, actor="panel",
                                 launcher=_run_launcher())
            except HTTPException:
                raise
            if hx:
                if out["status"] == "cancelled":
                    row = _run_row_response(request, s, run_id)
                    return _toast(row, f"运行 Run #{run_id} 已取消 cancelled")
                if out["status"] == "not_found":
                    return _toast(HTMLResponse(""), f"run {run_id} not found", "err")
                return _toast(_run_row_response(request, s, run_id),
                              f"运行 Run #{run_id} 已结束，未取消 already finished "
                              f"({out.get('current_status')})", "err")
        finally:
            s.close()
        if out["status"] == "cancelled":
            return RedirectResponse("/panel/runs", status_code=303)
        if out["status"] == "not_found":
            raise HTTPException(404, f"run {run_id} not found")
        raise HTTPException(409, f"run {run_id} already finished "
                                 f"({out.get('current_status')}); nothing cancelled")

    @app.post("/panel/clusters/{cluster_id}/capacity")
    async def cluster_capacity(cluster_id: int, request: Request):
        """Edit a cluster's max-concurrent-open-runs; effective next tick."""
        form = await request.form()
        hx = request.headers.get("hx-request") == "true"
        try:
            value = int(form.get("capacity", ""))
        except (TypeError, ValueError):
            if hx:
                return _toast(HTMLResponse("", status_code=400),
                              "capacity 必须是整数 must be an integer", "err")
            raise HTTPException(400, "capacity must be an integer")
        if value < 0:
            if hx:
                return _toast(HTMLResponse("", status_code=400),
                              "capacity 必须 >= 0 must be >= 0", "err")
            raise HTTPException(400, "capacity must be >= 0")
        s = _session()
        try:
            c = s.get(Cluster, cluster_id)
            if c is None:
                if hx:
                    return _toast(HTMLResponse(""),
                                  f"cluster {cluster_id} not found", "err")
                raise HTTPException(404, f"cluster {cluster_id} not found")
            c.capacity = value
            s.commit()
            if hx:
                row = next((f for f in _snapshot.fleet_health(s)
                            if f["id"] == cluster_id), None)
                resp = (templates.TemplateResponse(request, "_fleet_row.html",
                                                   {"f": row}) if row
                        else HTMLResponse(""))
                return _toast(resp, f"{c.name} 容量已保存 capacity saved: {value}")
        finally:
            s.close()
        return RedirectResponse("/panel", status_code=303)

    # ── platform sources & control (crawl-platform 4.1/4.2) ──────────────────
    # Same tables as the platform_* MCP tools (crawl_sources / crawl_runs /
    # pending_runs); the write operations are the shared functions from
    # platform_tools so the two entrances enforce identical guardrails.
    PLATFORM_HEALTH_FILTERS = ("stalled", "lit", "unlit")

    def _source_row_response(request: Request, s, source: str):
        row = next((r for r in _snapshot.platform_sources(s)
                    if r["source"] == source), None)
        if row is None:
            return None
        return templates.TemplateResponse(
            request, "_source_row.html", {"s": row})

    def _platform_run_row_response(request: Request, s, run_id: int):
        row = next((r for r in _snapshot.platform_runs(s, limit=500)
                    if r["id"] == run_id), None)
        if row is None:
            return None
        return templates.TemplateResponse(
            request, "_platform_run_row.html", {"r": row})

    def _pending_row_response(request: Request, s, pending_id: int):
        p = s.get(PendingRun, pending_id)
        if p is None:
            return None
        return templates.TemplateResponse(
            request, "_pending_row.html", {"p": p.toDict()})

    @app.get("/panel/sources", response_class=HTMLResponse)
    def platform_sources_page(request: Request, site: str = "",
                              health: str = ""):
        """Platform source inventory: every crawl_sources row with site,
        schedule (未点亮 when NULL), enabled, latest crawl_runs fact, active
        pending count, trigger button. Filters: site / health state."""
        s = _session()
        try:
            rows = _snapshot.platform_sources(s, site=site or None)
            if health == "stalled":
                rows = [r for r in rows if r["stalled"]]
            elif health == "lit":
                rows = [r for r in rows if r["schedule"]]
            elif health == "unlit":
                rows = [r for r in rows if not r["schedule"]]
            sites = [r[0] for r in s.query(CrawlSite.id).order_by(CrawlSite.id).all()]
            ctx = {"sources": rows, "site": site, "health": health,
                   "sites": sites}
            # health chips / site filter swap just the results region
            if request.headers.get("hx-request") == "true":
                return templates.TemplateResponse(
                    request, "partial_sources_results.html", ctx)
            return templates.TemplateResponse(request, "sources.html", ctx)
        finally:
            s.close()

    @app.get("/panel/sources/{source}", response_class=HTMLResponse)
    def platform_source_detail(request: Request, source: str):
        s = _session()
        try:
            src = s.get(CrawlSource, source)
            if src is None:
                raise HTTPException(404, f"source {source} not found")
            row = next((r for r in _snapshot.platform_sources(s)
                        if r["source"] == source), None)
            runs = _snapshot.platform_runs(s, source=source, limit=20)
            pending = [
                p.toDict() for p in (
                    s.query(PendingRun)
                    .filter(PendingRun.source == source,
                            PendingRun.status.in_(("pending", "claimed")))
                    .order_by(PendingRun.id.desc()).all())
            ]
            # discovery-pipeline provenance (query-time fact, no FK): the
            # latest approved manifest whose source_name equals this source
            pipeline = _snapshot.source_pipeline_link(s, source)
            return templates.TemplateResponse(
                request, "source_detail.html",
                {"src": src.toDict(), "health": row,
                 "runs": runs, "pending": pending, "pipeline": pipeline})
        finally:
            s.close()

    @app.post("/panel/sources/{source}/trigger")
    def platform_source_trigger(source: str, request: Request):
        """Queue an immediate run: insert pending_runs (requested_by='panel').
        Validation is the shared platform_tools.trigger_platform_run —
        unregistered / disabled / single-flight refusals carry clear text."""
        hx = request.headers.get("hx-request") == "true"
        s = _session()
        try:
            out = trigger_platform_run(s, source, requested_by="panel")
            if out["status"] == "triggered":
                msg = (f"{source} 已触发 queued: pending #{out['pending_id']} "
                       f"(site {out.get('site') or '—'})")
                if hx:
                    return _toast(_source_row_response(request, s, source), msg)
                return RedirectResponse(f"/panel/sources/{source}",
                                        status_code=303)
            msg = f"{source} 未触发 not triggered: {out['reason']}"
            if out["status"] == "not_found":
                if hx:
                    return _toast(HTMLResponse(""), msg, "err")
                raise HTTPException(404, out["reason"])
            # refused (disabled / single-flight) or a rejected insert
            if hx:
                return _toast(_source_row_response(request, s, source) or
                              HTMLResponse(""), msg, "err")
            if out["status"] == "error":
                raise HTTPException(400, out["reason"])
            raise HTTPException(409, out["reason"])
        finally:
            s.close()

    @app.post("/panel/runs/platform/{run_id}/cancel")
    def platform_run_cancel(run_id: int, request: Request):
        """Request cancellation of a RUNNING platform run: CAS-set
        cancel_requested; the runner closes its own row as cancelled."""
        hx = request.headers.get("hx-request") == "true"
        s = _session()
        try:
            out = cancel_platform_run(s, run_id)
            if hx:
                if out["status"] == "cancel_requested":
                    return _toast(
                        _platform_run_row_response(request, s, run_id),
                        f"平台运行 Platform run #{run_id} 已请求取消 cancel "
                        f"requested — 运行器将在下个检查点退出 the runner "
                        f"exits at its next checkpoint")
                if out["status"] == "not_found":
                    return _toast(HTMLResponse(""),
                                  f"platform run {run_id} not found", "err")
                return _toast(
                    _platform_run_row_response(request, s, run_id)
                    or HTMLResponse(""),
                    f"运行 Run #{run_id} 已结束，未取消 already finished "
                    f"({out.get('current_status')})", "err")
        finally:
            s.close()
        if out["status"] == "cancel_requested":
            return RedirectResponse("/panel/runs", status_code=303)
        if out["status"] == "not_found":
            raise HTTPException(404, f"platform run {run_id} not found")
        raise HTTPException(409, f"platform run {run_id} already finished "
                                 f"({out.get('current_status')}); nothing cancelled")

    @app.post("/panel/pending/{pending_id}/cancel")
    def platform_pending_cancel(pending_id: int, request: Request):
        """Cancel a pending/claimed pending_runs row (CAS; terminal rows 409)."""
        hx = request.headers.get("hx-request") == "true"
        s = _session()
        try:
            back = None
            p = s.get(PendingRun, pending_id)
            if p is not None:
                back = f"/panel/sources/{p.source}"
            out = cancel_pending_run(s, pending_id)
            if hx:
                if out["status"] == "cancelled":
                    return _toast(
                        _pending_row_response(request, s, pending_id),
                        f"待运行务 Pending #{pending_id} 已取消 cancelled")
                if out["status"] == "not_found":
                    return _toast(HTMLResponse(""),
                                  f"pending run {pending_id} not found", "err")
                return _toast(
                    _pending_row_response(request, s, pending_id)
                    or HTMLResponse(""),
                    f"待运行务 Pending #{pending_id} 已是终态，未取消 already "
                    f"{out.get('current_status')}", "err")
        finally:
            s.close()
        if out["status"] == "cancelled":
            return RedirectResponse(back or "/panel/sources", status_code=303)
        if out["status"] == "not_found":
            raise HTTPException(404, f"pending run {pending_id} not found")
        raise HTTPException(409, f"pending run {pending_id} already "
                                 f"{out.get('current_status')}; nothing cancelled")

    @app.get("/panel/partials/platform", response_class=HTMLResponse)
    def partial_platform(request: Request):
        """Polled platform-source health roll-up on the cockpit (15s, degrades
        independently like every home partial)."""
        try:
            s = _session()
            try:
                h = _snapshot.platform_health(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_platform.html", {"h": h})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    # ── source-discovery funnel (harness-platform-integration 2.2/2.3) ──────
    # Read-only view over the central pipeline tables (discoveries /
    # candidates / analyses / manifests). The page shell never touches the
    # DB; the polled partial carries the data and degrades independently.
    # Approval happens on the harness tool surface (agent-operated) — the
    # panel only observes the funnel (spec source-discovery-pipeline).
    @app.get("/panel/funnel", response_class=HTMLResponse)
    def funnel_page(request: Request):
        return templates.TemplateResponse(
            request, "funnel.html", {"poll_seconds": POLL_SECONDS})

    @app.get("/panel/partials/funnel", response_class=HTMLResponse)
    def partial_funnel(request: Request):
        """Polled funnel stage counts + latest approval queue (15s, degrades
        like every home partial)."""
        try:
            s = _session()
            try:
                f = _snapshot.discovery_funnel(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_funnel.html", {"f": f})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    # ── authenticated-crawling panel (session-pool 4.1) ─────────────────────
    # The Console login操作面 (login-station-console 3.3): the identity x
    # health matrix with one-click login-station launch, the login-required
    # queue, the event stream, multi-account registration and the station
    # board. The page shell never queries; the polled partial carries the data
    # and degrades independently. Logins happen ON the login station — this
    # surface orchestrates and observes (spec authenticated-crawling).
    @app.get("/panel/auth", response_class=HTMLResponse)
    def auth_page(request: Request):
        return templates.TemplateResponse(
            request, "auth.html", {"poll_seconds": POLL_SECONDS})

    @app.get("/panel/partials/auth", response_class=HTMLResponse)
    def partial_auth(request: Request):
        """Polled auth panel: identity matrix grouped by source, the
        login-required queue and the latest identity events (15s, degrades
        like every home partial); login-station board + registration sources
        ride along (login-station-console 3.3)."""
        try:
            s = _session()
            try:
                pool = _snapshot.identity_pool(s)
                queue = _snapshot.login_queue(s)
                events = _snapshot.identity_events(s, limit=20)
                profiles = dict(
                    s.query(CrawlSource.source, CrawlSource.auth_profile).all())
                stations = station_ops.station_status(s, limit=10)
                station_sources = [
                    {"source": r.source, "auth_profile": r.auth_profile}
                    for r in (s.query(CrawlSource)
                              .filter(CrawlSource.auth_profile.isnot(None))
                              .order_by(CrawlSource.source).all())
                    if r.auth_profile]
            finally:
                s.close()
            grouped: dict[str, list[dict]] = {}
            for r in pool:
                grouped.setdefault(r["source"], []).append(r)
            groups = [{"source": src,
                       "auth_profile": profiles.get(src),
                       "identities": ids}
                      for src, ids in sorted(grouped.items())]
            return templates.TemplateResponse(
                request, "partial_auth.html",
                {"groups": groups, "queue": queue, "events": events,
                 "stations": stations, "station_sources": station_sources,
                 "summary": {
                     "total": len(pool),
                     "active": sum(1 for r in pool if r["status"] == "active"),
                     "login_required": len(queue),
                     "leased": sum(1 for r in pool if r["leased"])}})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    # ── login-station console (login-station-console 3.2/3.3) ──────────────
    # One-click login: the matrix's login_required rows post here; the HTMX
    # response swaps the observation view (an iframe on the reverse-proxied
    # noVNC page) into #station-view and refreshes the polled partial. The
    # observation channel itself never bypasses the panel gate above.
    @app.post("/panel/auth/station/launch")
    async def station_launch(request: Request):
        """Launch a login station: ensure the identity (login_required +
        egress), create the station Job+Service, embed the observation view."""
        form = await request.form()
        source = (form.get("source") or "").strip()
        alias = (form.get("account_alias") or "").strip()
        hx = request.headers.get("hx-request") == "true"
        if not source or not alias:
            if hx:
                return _toast(HTMLResponse("", status_code=400),
                              "源与账号别名必填 source and account_alias required",
                              "err")
            raise HTTPException(400, "source and account_alias are required")
        s = _session()
        try:
            ensured = station_ops.ensure_identity_with_egress(
                s, source, alias, requested_by="panel")
            if ensured.get("status") != "queued":
                msg = f"{source}/{alias} 未登记 not registered: {ensured.get('reason')}"
                if hx:
                    return _toast(HTMLResponse(""), msg, "err")
                raise HTTPException(400, ensured.get("reason", "invalid"))
            out = station_ops.create_station(s, source, alias,
                                             client=_station_client())
            if out["status"] != "launched":
                msg = (f"{source}/{alias} 拉起失败 launch failed: "
                       f"{out.get('reason')}")
                if hx:
                    return _toast(HTMLResponse(""), msg, "err")
                raise HTTPException(502, out.get("reason", "launch failed"))
            stations = station_ops.station_status(s)
            st = next(x for x in stations if x["id"] == out["station_id"])
        finally:
            s.close()
        if not hx:
            return RedirectResponse("/panel/auth", status_code=303)
        resp = templates.TemplateResponse(
            request, "_station_view.html",
            {"st": st, "vnc_url": station_ops.station_vnc_path(out["station_id"])})
        resp.headers["HX-Trigger"] = json.dumps({
            "toast": {"message": (f"登录站已拉起 station #{out['station_id']} "
                                  f"launched — 请在观察窗内完成登录 complete "
                                  f"the login in the observation view"),
                      "level": "ok"},
            "auth-refresh": {}})
        return resp

    @app.post("/panel/auth/identities")
    async def auth_identity_create(request: Request):
        """多账号登记 (spec: Console 登录操作面): register an identity and
        auto-assign its egress; the fragment shows the assigned binding."""
        form = await request.form()
        source = (form.get("source") or "").strip()
        alias = (form.get("account_alias") or "").strip()
        hx = request.headers.get("hx-request") == "true"
        if not source or not alias:
            if hx:
                return _toast(HTMLResponse("", status_code=400),
                              "源与账号别名必填 source and alias required", "err")
            raise HTTPException(400, "source and account_alias are required")
        s = _session()
        try:
            out = station_ops.ensure_identity_with_egress(
                s, source, alias, requested_by="panel")
        finally:
            s.close()
        if out.get("status") != "queued":
            msg = f"{source}/{alias} 未登记 not registered: {out.get('reason')}"
            if hx:
                return _toast(HTMLResponse(""), msg, "err")
            raise HTTPException(400, out.get("reason", "invalid"))
        if not hx:
            return RedirectResponse("/panel/auth", status_code=303)
        resp = templates.TemplateResponse(
            request, "_identity_created.html",
            {"out": out,
             "proxy_masked": station_ops.mask_proxy_url(out.get("proxy_url"))})
        resp.headers["HX-Trigger"] = json.dumps({
            "toast": {"message": (f"{source}/{alias} 已登记 registered — 出口 "
                                  f"egress {out.get('egress_ref') or '未分配 none'}"),
                      "level": "ok"},
            "auth-refresh": {}})
        return resp

    @app.get("/panel/auth/station/{station_id}/view", response_class=HTMLResponse)
    def station_view(request: Request, station_id: int):
        """The observation-view fragment for a live station (htmx target of
        the board's 观察窗 button)."""
        s = _session()
        try:
            stations = station_ops.station_status(s)
            st = next((x for x in stations if x["id"] == station_id), None)
        finally:
            s.close()
        if st is None or not st["live"]:
            return HTMLResponse(_station_unavailable(station_id),
                                status_code=404)
        return templates.TemplateResponse(
            request, "_station_view.html",
            {"st": st, "vnc_url": station_ops.station_vnc_path(station_id)})

    @app.post("/panel/auth/station/{station_id}/reclaim")
    def station_reclaim(station_id: int, request: Request):
        """Operator reclaim: tear the station's Job+Service down and close the
        row (deadline backstop covers the overdue case automatically)."""
        hx = request.headers.get("hx-request") == "true"
        s = _session()
        try:
            out = station_ops.reclaim_station(s, station_id,
                                              client=_station_client(),
                                              actor="panel")
            if out["status"] == "not_found":
                if hx:
                    return _toast(HTMLResponse(""),
                                  f"station {station_id} not found", "err")
                raise HTTPException(404, f"station {station_id} not found")
            stations = station_ops.station_status(s)
            st = next((x for x in stations if x["id"] == station_id), None)
        finally:
            s.close()
        if not hx:
            return RedirectResponse("/panel/auth", status_code=303)
        msg = (f"登录站 station #{station_id} 已回收 reclaimed"
               if out["status"] == "reclaimed" else
               f"登录站 station #{station_id} 已结束 already "
               f"{out.get('current_status')}；集群对象已清理 objects cleaned")
        row = (templates.TemplateResponse(request, "_station_row.html",
                                          {"t": st})
               if st is not None else HTMLResponse(""))
        return _toast(row, msg)

    @app.get("/panel/auth/station/{station_id}/vnc/{path:path}")
    async def station_vnc_proxy(station_id: int, path: str, request: Request):
        """Reverse proxy to the station's noVNC static page (and any asset
        under it) — the only HTTP path to a station's web UI. Panel-gated
        above; stations are never exposed directly."""
        import httpx

        upstream = _station_upstream(station_id)
        if upstream is None:
            return HTMLResponse(_station_unavailable(station_id),
                                status_code=404)
        url = f"http://{upstream}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        try:
            # trust_env=False: the station Service is cluster-internal — an
            # ambient HTTP(S)_PROXY (dev box / egress-restricted pod) must
            # never intercept the observation channel
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, read=60.0),
                    trust_env=False) as hc:
                up = await hc.get(
                    url,
                    headers={"accept": request.headers.get("accept", "*/*")})
        except httpx.HTTPError as e:  # noqa: BLE001 - friendly, never a 500
            logger.warning("station %s http proxy failed: %s", station_id, e)
            return HTMLResponse(_station_unreachable(station_id, e),
                                status_code=502)
        return Response(
            content=up.content, status_code=up.status_code,
            media_type=up.headers.get("content-type", "application/octet-stream"),
            headers={"cache-control": "no-store"})

    def _station_ws_authorized(websocket: WebSocket) -> bool:
        """The panel gate for the websocket scope (the http middleware cannot
        see websockets): same primitives — PANEL_TOKEN (?token= / header /
        cookie) or a valid OIDC session cookie (with the role re-check,
        panel-role-gate D2); an unconfigured gate stays open, exactly like
        the http gate."""
        token = os.environ.get("PANEL_TOKEN")
        if token and (websocket.query_params.get("token") == token
                      or websocket.headers.get("x-panel-token") == token
                      or websocket.cookies.get("panel_token") == token):
            return True
        sess = _auth.read_session(
            websocket.cookies.get(_auth.SESSION_COOKIE))
        if sess is not None:
            role = _auth.required_role()
            if role is not None:
                if role in sess["roles"]:
                    return True
            else:
                allowed = _auth.allow_list()
                if allowed is None or sess["sub"] in allowed:
                    return True
        if token is None and _auth.logto_config() is None:
            return True  # no gate configured — open panel (http parity)
        return False

    async def _relay_station_ws(websocket: WebSocket, station_id: int) -> None:
        """Bidirectional websocket relay: browser noVNC <-> the station pod's
        websockify. Hand-rolled two-pump relay (websockets client on the
        upstream side, starlette WebSocket on the panel side) — no extra
        framework dependency beyond the already-vendored websockets lib."""
        import asyncio

        await websocket.accept()
        if not _station_ws_authorized(websocket):
            await websocket.close(code=4401, reason="panel auth required")
            return
        upstream = _station_upstream(station_id)
        if upstream is None:
            await websocket.close(code=4404, reason="station not available")
            return
        import websockets

        try:
            async with websockets.connect(
                    f"ws://{upstream}/websockify",
                    open_timeout=10,
                    proxy=None,  # cluster-internal upstream; ignore ambient proxy env
                ) as up:

                async def _down() -> None:
                    async for message in up:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)

                async def _up() -> None:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if msg.get("text") is not None:
                            await up.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await up.send(msg["bytes"])

                done, pending = await asyncio.wait(
                    [asyncio.create_task(_down()),
                     asyncio.create_task(_up())],
                    return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    t.exception()  # surface relay errors to the event loop log
                try:
                    await websocket.close()
                except Exception:  # noqa: BLE001 - peer may have closed first
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - station unreachable / dropped
            logger.warning("station %s ws relay failed: %s", station_id, e)
            try:
                await websocket.close(
                    code=1014,
                    reason=f"station unreachable ({type(e).__name__})")
            except Exception:  # noqa: BLE001
                pass

    @app.websocket("/panel/auth/station/{station_id}/websockify")
    async def station_websockify(websocket: WebSocket, station_id: int):
        """noVNC data channel (RFB-over-websocket), relayed to the station."""
        await _relay_station_ws(websocket, station_id)

    @app.websocket("/panel/auth/station/{station_id}/vnc/websockify")
    async def station_websockify_nested(websocket: WebSocket,
                                        station_id: int):
        """Same relay at the vnc.html-relative path noVNC's default
        ``path=websockify`` resolves to from the embedded page."""
        await _relay_station_ws(websocket, station_id)

    # ── editor ─────────────────────────────────────────────────────────────
    def _editor_context(s, policy: CrawlPolicy | None):
        selected = set(policy.concept_ids) if policy else set()
        return {
            "policy": policy.toDict() if policy else None,
            "concept_groups": _concept_groups(s),
            "selected": selected,
            "entity_types": _entity_types(s),
            "frequencies": FREQUENCIES, "modes": MODES,
            "date_policy_modes": DATE_POLICY_MODES,
            "default_cron": "0 6 * * *",
        }

    @app.get("/panel/policies/new", response_class=HTMLResponse)
    def policy_new(request: Request, msg: str = "", err: str = ""):
        s = _session()
        try:
            ctx = _editor_context(s, None)
            ctx.update(msg=msg, err=err)
            return templates.TemplateResponse(request, "policy_edit.html", ctx)
        finally:
            s.close()

    @app.get("/panel/policies/template", response_class=HTMLResponse)
    def policy_template(request: Request, frequency: str = "daily"):
        """Frequency-driven policy template (panel-ops-console, design D7):
        pre-selects the hygiene-filtered concepts of that frequency and
        proposes the matching cron + date policy. Input aid only — the saved
        artifact is an ordinary policy row."""
        s = _session()
        try:
            concepts = _template_concepts(s, frequency)
            pseudo = {
                "frequency": frequency, "mode": "per_date", "enabled": True,
                "cron_expr": _TEMPLATE_CRON.get(frequency, "0 6 * * *"),
                "date_policy": ({"mode": "trailing", "days": 1}
                                if frequency == "daily" else {"mode": "since_last"}),
            }
            ctx = _editor_context(s, None)
            ctx.update(policy=pseudo, is_template=True,
                       template_frequency=frequency,
                       selected={c.id for c in concepts})
            return templates.TemplateResponse(request, "policy_edit.html", ctx)
        finally:
            s.close()

    @app.get("/panel/policies/{policy_id}", response_class=HTMLResponse)
    def policy_edit(request: Request, policy_id: int):
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            return templates.TemplateResponse(request, "policy_edit.html", _editor_context(s, p))
        finally:
            s.close()

    @app.post("/panel/policies/save")
    async def policy_save(request: Request):
        form = await request.form()
        payload = _policy_from_form(form)
        if not payload["name"] or not payload["concept_ids"]:
            raise HTTPException(400, "name and at least one concept are required")
        s = _session()
        try:
            pid = form.get("policy_id")
            if pid:
                p = _policy_or_404(s, int(pid))
                for k, v in payload.items():
                    setattr(p, k, v)
            else:
                s.add(CrawlPolicy(**payload))
            s.commit()
        finally:
            s.close()
        return RedirectResponse("/panel/policies", status_code=303)

    def _policy_row_response(request: Request, s, pid: int):
        p = s.query(CrawlPolicy).get(pid)
        if p is None:
            return None
        return templates.TemplateResponse(
            request, "_policy_row.html", {"p": p.toDict()})

    @app.post("/panel/policies/{policy_id}/toggle")
    def policy_toggle(policy_id: int, request: Request):
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            p.enabled = not p.enabled
            s.commit()
            if request.headers.get("hx-request") == "true":
                state = "已启用 enabled" if p.enabled else "已停用 disabled"
                return _toast(_policy_row_response(request, s, policy_id),
                              f"{p.name} {state}")
        finally:
            s.close()
        return RedirectResponse("/panel/policies", status_code=303)

    @app.post("/panel/policies/{policy_id}/delete")
    def policy_delete(policy_id: int, request: Request):
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            s.delete(p)
            s.commit()
            if request.headers.get("hx-request") == "true":
                # empty 200 body removes the swapped row
                return _toast(HTMLResponse(""), f"{p.name} 已删除 deleted")
        finally:
            s.close()
        return RedirectResponse("/panel/policies", status_code=303)

    @app.post("/panel/policies/{policy_id}/run-now")
    def policy_run_now(policy_id: int, request: Request):
        from fd_open_data_mcp.refresh.reconciler import _default_launcher, launch_policy

        hx = request.headers.get("hx-request") == "true"
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            result = launch_policy(s, p, _default_launcher())
            if hx:
                if result.get("status") == "launched":
                    return _toast(_policy_row_response(request, s, policy_id),
                                  f"{p.name} 已触发运行 launched "
                                  f"(run #{result.get('run_id')})")
                return _toast(_policy_row_response(request, s, policy_id),
                              f"{p.name} 未触发 not launched: {result.get('reason')}",
                              "err")
        finally:
            s.close()
        return RedirectResponse(f"/panel/runs?policy_id={policy_id}", status_code=303)

    # ── estimate preview (htmx partial) ────────────────────────────────────
    def _compile_plan(s, payload: dict):
        """Compile a payload (as produced by _policy_from_form) into a plan —
        shared by the estimate preview and the ad-hoc launch."""
        from fd_open_data_mcp.crawl.plan import EntityScope
        from fd_open_data_mcp.crawl.planner import plan_crawl
        from fd_open_data_mcp.refresh.reconciler import build_date_range

        class _P:  # transient policy-like for build_date_range
            date_policy = payload["date_policy"]
            frequency = payload["frequency"]
        dr, since_last = build_date_range(_P(), dt.date.today())
        return plan_crawl(
            s, payload["concept_ids"],
            EntityScope(entity_type=payload["entity_type"], entity_ids=payload["entity_ids"]),
            dr, since_last=since_last, source_filter=payload["source_filter"],
            mode=payload["mode"])

    @app.post("/panel/estimate", response_class=HTMLResponse)
    async def estimate(request: Request):
        payload = _policy_from_form(await request.form())
        s = _session()
        try:
            from fd_open_data_mcp.refresh.reconciler import (
                POLICY_MAX_FETCHES, estimate_fetches)

            plan = _compile_plan(s, payload)
            est = estimate_fetches(s, plan)
            return templates.TemplateResponse(
                request, "estimate.html", {
                    "estimate": est, "policy_max": POLICY_MAX_FETCHES,
                    "mode": payload["mode"],
                    "n_concepts": len(plan.wanted_concepts),
                    "unroutable": plan.unroutable, "unmapped": plan.unmapped})
        except Exception as e:  # noqa: BLE001 - the preview must never 500 the editor
            return HTMLResponse(f"<span style='color:#c0392b'>estimate failed: {e}</span>")
        finally:
            s.close()

    # ── ad-hoc one-off crawl (panel-ops-console) ─────────────────────────────
    @app.post("/panel/crawl/adhoc")
    async def crawl_adhoc(request: Request):
        """Run a plan draft once, without creating a policy (design D3)."""
        from urllib.parse import quote

        from fd_open_data_mcp.refresh.runs import launch_adhoc

        payload = _policy_from_form(await request.form())
        s = _session()
        try:
            plan = _compile_plan(s, payload)
            result = launch_adhoc(s, plan, _run_launcher())
        except Exception as e:  # noqa: BLE001 - bad form input: show, don't 500
            return RedirectResponse(f"/panel/policies/new?err={quote(str(e))}",
                                    status_code=303)
        finally:
            s.close()
        if result["status"] == "launched":
            return RedirectResponse("/panel/runs?status=running", status_code=303)
        return RedirectResponse(
            f"/panel/policies/new?err={quote(result['reason'])}", status_code=303)

    # ── proxy operations (panel-ops-console) ─────────────────────────────────
    @app.get("/panel/proxy", response_class=HTMLResponse)
    def proxy_page(request: Request, msg: str = "", err: str = ""):
        s = _session()
        try:
            proxies = s.query(Proxy).order_by(Proxy.provider, Proxy.id).all()
            providers: dict[str, dict] = {}
            for p in proxies:
                bucket = providers.setdefault(p.provider or "(unowned)", {
                    "name": p.provider or "(unowned)", "active": 0, "retired": 0})
                bucket["retired" if p.status == "retired" else "active"] += 1
            limits = s.query(SourceRateLimit).order_by(SourceRateLimit.source).all()
            rules = (s.query(BanRule)
                     .order_by(BanRule.source, BanRule.priority.desc()).limit(200).all())
            return templates.TemplateResponse(request, "proxy.html", {
                "msg": msg, "err": err,
                "poll_seconds": POLL_SECONDS,
                "mgmt_ready": _proxy_control_ready(),
                "providers": sorted(providers.values(), key=lambda b: b["name"]),
                "proxies": [
                    {"id": p.id, "scheme": p.scheme, "ip": p.ip, "port": p.port,
                     "auth_masked": _mask_auth(p.auth), "status": p.status,
                     "label": p.label, "provider": p.provider or "(unowned)"}
                    for p in proxies],
                "rate_limits": [
                    {"source": r.source, "max_qps": r.max_qps,
                     "max_concurrent": r.max_concurrent,
                     "throttled": (r.max_qps or 0) > 0}
                    for r in limits],
                "ban_rules": [
                    {"source": r.source, "rule_type": r.rule_type,
                     "pattern": r.pattern[:60], "classification": r.classification,
                     "enabled": r.enabled}
                    for r in rules],
            })
        finally:
            s.close()

    @app.get("/panel/partials/proxy", response_class=HTMLResponse)
    def partial_proxy(request: Request):
        """Polled circuit-health matrix: per-(source, proxy) state from the
        shared cold table (near-realtime — hot state lives in proxy-redis)."""
        try:
            s = _session()
            try:
                rows = (s.query(SourceProxyHealth)
                        .order_by(SourceProxyHealth.source, SourceProxyHealth.proxy_id)
                        .limit(500).all())
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_proxy.html", {"circuits": [h.toDict() for h in rows]})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.post("/panel/proxy/import")
    async def proxy_import(request: Request):
        form = await request.form()
        try:
            out = _proxy_control("POST", "/management/proxies/import", {
                "provider": form.get("provider") or "paid-static",
                "text": form.get("text", ""),
            })
        except RuntimeError as e:
            return _proxy_redirect(err=str(e))
        return _proxy_redirect(msg=(
            f"imported {out.get('imported', 0)}, skipped {out.get('skipped', 0)}"
            + (f", updated {out['updated']}" if out.get("updated") else "")))

    @app.post("/panel/proxy/circuits/reset")
    async def proxy_circuit_reset(request: Request):
        form = await request.form()
        try:
            _proxy_control("POST", "/management/circuits/reset", {
                "source": form.get("source"), "proxy_id": int(form.get("proxy_id", 0)),
                "actor": form.get("actor") or "panel"})
        except (RuntimeError, TypeError, ValueError) as e:
            return _proxy_redirect(err=str(e))
        return _proxy_redirect(msg=f"circuit {form.get('source')}/{form.get('proxy_id')} -> HALF_OPEN")

    @app.post("/panel/proxy/rate-limits/{source}")
    async def proxy_rate_limit(source: str, request: Request):
        form = await request.form()
        raw_qps = (form.get("max_qps") or "").strip()
        try:
            max_qps = float(raw_qps) if raw_qps else 0.0  # blank = no throttling
            _proxy_control("PUT", f"/management/rate-limits/{source}", {
                "max_qps": max_qps,
                "max_concurrent": int(form.get("max_concurrent") or 4)})
        except (RuntimeError, TypeError, ValueError) as e:
            return _proxy_redirect(err=str(e))
        return _proxy_redirect(msg=f"rate limit for {source} saved")

    # NOTE: the dynamic {proxy_id} route goes LAST so the literal paths above
    # are never shadowed by a partial int-converter match.
    @app.post("/panel/proxy/{proxy_id}/status")
    async def proxy_set_status(proxy_id: int, request: Request):
        form = await request.form()
        try:
            out = _proxy_control("POST", f"/management/proxies/{proxy_id}/status", {
                "status": form.get("status"), "actor": form.get("actor") or "panel"})
        except RuntimeError as e:
            return _proxy_redirect(err=str(e))
        if out.get("status") == "not_found":
            return _proxy_redirect(err=f"proxy {proxy_id} not found")
        return _proxy_redirect(msg=f"proxy {proxy_id} -> {out.get('proxy_status')}")

    return app


app = create_app()
