"""Login-station orchestration (login-station-console, tasks 3.1/3.4).

The Console-side half of the login-station contract: a login station is a
temporary headful-browser Job (image fd-industry-runner, script
``scripts/login_station.py --station <src> <alias>``) plus a ClusterIP
Service exposing its noVNC/websockify port (6080). The panel launches,
observes (reverse proxy, panel/app.py) and reclaims stations through this
module; the station runtime itself reports lifecycle transitions
(launching -> waiting_operator -> completed/failed) into the same
``crawl_login_stations`` row it was handed via ``STATION_ID``.

Everything that talks to the cluster goes through an injectable
``StationK8sClient`` (create/delete/list Job+Service) so tests pass a fake
and no pod is ever started — the same launcher-abstraction discipline as
refresh/reconciler.

The central table is created by the fd-industry-data side; ``STATION_DDL``
below is the shared DDL (kept character-identical to
fd-industry-data/scripts/login_station.py::STATION_DDL) and the SQLAlchemy
mirror model lives in models.CrawlLoginStation.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    CrawlIdentity, CrawlIdentityEvent, CrawlLoginStation, CrawlSource, Proxy,
)

logger = logging.getLogger(__name__)

# ── station constants (mirror the runtime contract) ─────────────────────────
# Shared table DDL: character-identical to the fd-industry-data runtime's
# STATION_DDL so both sides of the contract create/see the same shape.
STATION_DDL = """
CREATE TABLE IF NOT EXISTS crawl_login_stations (
    id          bigserial PRIMARY KEY,
    identity_id bigint NOT NULL REFERENCES crawl_identities(id) ON DELETE CASCADE,
    source      text NOT NULL,
    account_alias text NOT NULL,
    status      text NOT NULL DEFAULT 'launching'
                CHECK (status IN ('launching','waiting_operator','completed',
                                  'failed','timeout','reclaimed')),
    proxy_url   text,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deadline_at timestamptz,
    finished_at timestamptz
);
"""

STATION_NAMESPACE = "fd-mcp"        # ns with the station-launcher Role (SA fd-panel)
STATION_PORT = 6080                 # station websockify/noVNC port (login_station.py WS_PORT)
STATION_DEADLINE_SECONDS = 3600     # 60 min default: row deadline, Job activeDeadline and runtime backstop
STATION_IMAGE_DEFAULT = "fd-industry-runner:latest"  # placeholder when STATION_IMAGE is unset
STATION_TERMINAL = ("completed", "failed", "timeout", "reclaimed")
STATION_OPEN = ("launching", "waiting_operator")

# Pre-arm defaults (change prearm-login-station): how close a scheduled
# authenticated run must be before the Console opens a login station for it,
# and how recently an identity must have been probed to count as fresh.
PREARM_MINUTES_DEFAULT = 45
PREARM_FRESH_HOURS_DEFAULT = 6
PREARM_INTERVAL_SECONDS_DEFAULT = 300


def _env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment; unset, blank or garbage
    degrades to ``default`` (a typo must never disarm the station window or
    the pre-arm loop)."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def station_deadline_seconds() -> int:
    """Station window (row deadline, Job activeDeadlineSeconds, runtime budget).

    Env FD_STATION_DEADLINE_SECONDS overrides; a 25-min window proved too short
    for a human to reach the observation view and finish a phone+password login
    (stations #16-#19, #26 died waiting, 2026-10-08/09)."""
    return _env_int("FD_STATION_DEADLINE_SECONDS", STATION_DEADLINE_SECONDS)


def prearm_minutes() -> int:
    """Pre-arm horizon: open a login station when a scheduled authenticated
    source fires within this many minutes (FD_PREARM_MINUTES)."""
    return _env_int("FD_PREARM_MINUTES", PREARM_MINUTES_DEFAULT)


def prearm_fresh_hours() -> int:
    """An identity counts as fresh while its last probe is younger than this
    many hours (FD_PREARM_FRESH_HOURS); also the panel's stale-session cutoff."""
    return _env_int("FD_PREARM_FRESH_HOURS", PREARM_FRESH_HOURS_DEFAULT)


def prearm_interval_seconds() -> int:
    """Cadence of the panel's background pre-arm pass (FD_PREARM_INTERVAL_SECONDS,
    default 300 — the planner is cheap and the window is tens of minutes)."""
    return _env_int("FD_PREARM_INTERVAL_SECONDS", PREARM_INTERVAL_SECONDS_DEFAULT)


# git sparse-checkout of spiders/ for the station Job's initContainer — the
# validated pattern of fd-industry-data k8s/dispatcher.yaml (one image ships
# git; the node pulls a single image).
STATION_CHECKOUT_SCRIPT = """
set -e
git clone --depth 1 --filter=blob:none --sparse \
  "https://FindDataTechnology:${GITEE_TOKEN}@gitee.com/FindDataTechnology/fd-industry-data.git" /content
cd /content
git sparse-checkout set spiders/ scripts/
git rev-parse HEAD > /content/.commit
"""

# The four secrets the cluster side pre-provisions in the station namespace
# (fd-mcp): central DB, RustFS object storage, session encryption key, and
# the gitee pull token for the checkout initContainer.
STATION_SECRET_ENV = [
    {"name": "FD_CRAWL_DB_URL",
     "valueFrom": {"secretKeyRef": {"name": "fd-industry-db", "key": "url"}}},
    {"name": "RUSTFS_ENDPOINT",
     "valueFrom": {"secretKeyRef": {"name": "fd-industry-rustfs", "key": "endpoint"}}},
    {"name": "RUSTFS_ACCESS_KEY",
     "valueFrom": {"secretKeyRef": {"name": "fd-industry-rustfs", "key": "access-key"}}},
    {"name": "RUSTFS_SECRET_KEY",
     "valueFrom": {"secretKeyRef": {"name": "fd-industry-rustfs", "key": "secret-key"}}},
    {"name": "PLATFORM_SESSION_KEY",
     "valueFrom": {"secretKeyRef": {"name": "platform-session-key", "key": "key"}}},
]


def station_image() -> str:
    """Station runtime image; ``STATION_IMAGE`` is set by the panel's deploy
    env — empty/unset degrades to the :latest placeholder."""
    return os.environ.get("STATION_IMAGE", "").strip() or STATION_IMAGE_DEFAULT


# ── time helpers (sqlite reads timestamps back naive; PG timestamptz too) ────
def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_aware(v: dt.datetime | None) -> dt.datetime | None:
    if v is None or v.tzinfo is not None:
        return v
    return v.replace(tzinfo=dt.timezone.utc)


def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


# ── egress resolution / assignment (mirror fd_industry_data.auth) ────────────
def proxy_url_for_ref(session: Session, egress_ref: str | None) -> str | None:
    """``proxy:<id>`` -> ``scheme://[user:pass@]ip:port``; None for unbound,
    unknown, retired or direct egress (a direct proxy is no station egress)."""
    if not (egress_ref or "").startswith("proxy:"):
        return None
    try:
        pid = int(egress_ref.split(":", 1)[1])
    except ValueError:
        return None
    p = session.get(Proxy, pid)
    if p is None or p.retired_at is not None or p.status == "retired":
        return None
    if p.ip == "direct" or p.port is None:
        return None
    cred = f"{p.auth}@" if p.auth else ""
    return f"{p.scheme}://{cred}{p.ip}:{p.port}"


def mask_proxy_url(url: str | None) -> str:
    """Panel-safe proxy URL (credentials never render in a response body)."""
    if not url:
        return "—"
    return re.sub(r"//[^@/]+@", "//••••@", url)


# Operator-facing display names for login sources: the observation modal
# headlines WHICH site the operator is logging into (a bare "rmfyalk / acct001"
# row read as an opaque code name). Unknown sources fall back to the raw name.
STATION_SOURCE_LABELS: dict[str, tuple[str, str]] = {
    "rmfyalk": ("人民法院案例库", "https://rmfyalk.court.gov.cn"),
}


def source_label(source: str) -> tuple[str, str]:
    """``(display_name, site_url)`` for a login source; unknown sources
    degrade to ``(source, "")``."""
    return STATION_SOURCE_LABELS.get(source, (source, ""))


def selectable_egresses(session: Session) -> list[dict]:
    """The egress choices an operator may pick for a login: every healthy
    pooled proxy (not retired, not direct, has a port), id-ascending.

    The auto-assignment policy ranks least-referenced first; this list is the
    manual override surface for it (operators know e.g. which egress the
    source's risk controls tolerate). ``url_masked`` never carries
    credentials."""
    rows = (session.query(Proxy)
            .filter(Proxy.retired_at.is_(None),
                    Proxy.status != "retired",
                    Proxy.ip != "direct",
                    Proxy.port.isnot(None))
            .order_by(Proxy.id).all())
    return [{"egress_ref": f"proxy:{p.id}",
             "label": p.label or f"{p.ip}:{p.port}",
             "provider": p.provider,
             "url_masked": mask_proxy_url(proxy_url_for_ref(
                 session, f"proxy:{p.id}"))}
            for p in rows]


def bind_egress_ref(session: Session, ident: CrawlIdentity, egress_ref: str,
                    now: dt.datetime, requested_by: str = "panel") -> dict:
    """Operator-chosen egress: bind ``ident`` to exactly this proxy, replacing
    any current binding. Unknown/retired/direct refs are refused (the caller
    surfaces the reason; nothing is written).

    Uniqueness follows the same rule the allocator enforces — one egress per
    account within a source — so a manual pick cannot silently collide with a
    sibling account: if another identity of the same source already holds the
    ref, the change is refused and reported."""
    if not (egress_ref or "").startswith("proxy:"):
        return {"status": "invalid", "reason": f"bad egress ref '{egress_ref}'"}
    try:
        pid = int(egress_ref.split(":", 1)[1])
    except ValueError:
        return {"status": "invalid", "reason": f"bad egress ref '{egress_ref}'"}
    p = session.get(Proxy, pid)
    if p is None or p.retired_at is not None or p.status == "retired":
        return {"status": "invalid",
                "reason": f"egress {egress_ref} unknown or retired"}
    if p.ip == "direct" or p.port is None:
        return {"status": "invalid",
                "reason": f"egress {egress_ref} is not a usable proxy"}
    conflict = (session.query(CrawlIdentity)
                .filter(CrawlIdentity.source == ident.source,
                        CrawlIdentity.egress_ref == egress_ref,
                        CrawlIdentity.id != ident.id).first())
    if conflict is not None:
        return {"status": "conflict",
                "reason": (f"egress {egress_ref} already bound to "
                           f"{ident.source}/{conflict.account_alias}")}
    ident.egress_ref = egress_ref
    session.add(CrawlIdentityEvent(
        identity_id=ident.id, kind="note",
        detail=(f"egress manually set to {egress_ref} by {requested_by} "
                f"({p.label or p.ip}:{p.port})"),
        created_at=now))
    session.commit()
    return {"status": "bound", "egress_ref": egress_ref,
            "proxy_url": proxy_url_for_ref(session, egress_ref)}


def _allocate_egress(session: Session, ident: CrawlIdentity,
                     now: dt.datetime) -> str | None:
    """Bind the identity to a proxy — the assign_egress policy of
    fd_industry_data.auth, ported to the ORM: healthy (not retired, not
    direct, has a port), not already bound to another identity of the SAME
    source, globally least-referenced, id ascending. Recorded as an event.

    Returns the new egress_ref, or None when the pool is dry (also evented)."""
    same_source_taken: set[int] = set()
    refs_count: dict[str, int] = {}
    for ref, n in (session.query(CrawlIdentity.egress_ref, func.count(CrawlIdentity.id))
                   .filter(CrawlIdentity.egress_ref.like("proxy:%"))
                   .group_by(CrawlIdentity.egress_ref).all()):
        refs_count[ref] = int(n)
    for (ref,) in (session.query(CrawlIdentity.egress_ref)
                   .filter(CrawlIdentity.source == ident.source,
                           CrawlIdentity.egress_ref.like("proxy:%")).all()):
        try:
            same_source_taken.add(int(ref.split(":", 1)[1]))
        except (ValueError, IndexError):
            continue
    best: tuple[Proxy, int] | None = None
    for p in (session.query(Proxy)
              .filter(Proxy.retired_at.is_(None),
                      Proxy.status != "retired",
                      Proxy.ip != "direct",
                      Proxy.port.isnot(None))
              .order_by(Proxy.id).all()):
        if p.id in same_source_taken:
            continue  # one egress per account within a source
        refs = refs_count.get(f"proxy:{p.id}", 0)
        if best is None or refs < best[1]:
            best = (p, refs)  # candidates scan in id order -> id-asc tiebreak
    if best is None:
        session.add(CrawlIdentityEvent(
            identity_id=ident.id, kind="note",
            detail="egress assignment: pool dry", created_at=now))
        session.commit()
        return None
    egress_ref = f"proxy:{best[0].id}"
    ident.egress_ref = egress_ref
    session.add(CrawlIdentityEvent(
        identity_id=ident.id, kind="note",
        detail=f"egress assigned {egress_ref}", created_at=now))
    session.commit()
    return egress_ref


def ensure_identity_with_egress(
    session: Session,
    source: str,
    account_alias: str,
    automation: str = "assisted",
    requested_by: str = "panel",
    now: dt.datetime | None = None,
    egress_ref: str | None = None,
) -> dict:
    """Register/reset an identity to ``login_required`` and make sure it has
    an egress binding — the one write every identity-creation entrance funnels
    through (panel form, MCP tool, station launch).

    Composition: auth_tools.request_identity_login does the row write (reset
    semantics + audit event); the egress step mirrors assign_egress
    (idempotent: an already-bound identity keeps its proxy). Returns the
    request_identity_login dict plus ``egress_ref`` / ``proxy_url``.

    ``egress_ref`` is the operator's explicit choice (``proxy:<id>``): it
    replaces any current binding and is validated by ``bind_egress_ref``
    (unknown/retired/cross-account-conflicting refs are refused and reported
    in ``status``) — the manual override for the auto-assignment policy."""
    from fd_open_data_mcp.auth_tools import request_identity_login  # lazy: auth_tools imports this module

    now = _as_aware(now) or _utcnow()
    out = request_identity_login(session, source, account_alias,
                                 automation=automation,
                                 requested_by=requested_by, now=now)
    if out.get("status") != "queued":
        return {**out, "egress_ref": None, "proxy_url": None}
    ident = session.get(CrawlIdentity, out["identity_id"])
    assert ident is not None  # request_identity_login just committed it
    if egress_ref:
        chosen = bind_egress_ref(session, ident, egress_ref, now,
                                 requested_by=requested_by)
        if chosen["status"] != "bound":
            return {**out, **chosen}
        return {**out, "egress_ref": chosen["egress_ref"],
                "proxy_url": chosen["proxy_url"]}
    if (ident.egress_ref or "").startswith("proxy:"):
        # idempotent: keep the existing binding (its assignment is already on
        # the audit trail)
        return {**out, "egress_ref": ident.egress_ref,
                "proxy_url": proxy_url_for_ref(session, ident.egress_ref)}
    ref = _allocate_egress(session, ident, now)
    return {**out, "egress_ref": ref,
            "proxy_url": proxy_url_for_ref(session, ref) if ref else None}


# ── k8s client (injectable; the panel pod's own SA drives the real one) ──────
class StationK8sClient:
    """K8s API client for login-station Jobs + Services in one namespace.

    Transport mirrors refresh.reconciler.K8sJobLauncher: the in-cluster
    service-account REST API when the SA token exists (the panel pod — SA
    fd-panel holds the station-launcher Role: create/get/list/delete jobs,
    services, pods), kubectl for local dev. Passed explicitly by callers so
    tests substitute a fake and nothing is ever really launched."""

    _SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
    _PLURAL = {"Job": ("apis/batch/v1", "jobs"),
               "Service": ("api/v1", "services")}

    def __init__(self, namespace: str | None = None):
        self.namespace = (namespace or os.environ.get("STATION_K8S_NAMESPACE")
                          or STATION_NAMESPACE)

    # -- transport ----------------------------------------------------------
    def _in_cluster(self) -> bool:
        return os.path.exists(f"{self._SA_DIR}/token")

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        import ssl
        import urllib.request

        with open(f"{self._SA_DIR}/token", encoding="utf-8") as fh:
            token = fh.read().strip()
        ctx = ssl.create_default_context(cafile=f"{self._SA_DIR}/ca.crt")
        req = urllib.request.Request(
            f"https://kubernetes.default.svc{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return json.load(r)

    def _kubectl(self, *args: str, input_text: str | None = None) -> str:
        import subprocess

        out = subprocess.run(  # noqa: S603
            ["kubectl", "-n", self.namespace, *args],
            input=input_text, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(
                f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout

    # -- interface used by station_ops / the panel ---------------------------
    def create(self, manifest: dict) -> dict:
        """Create the manifest's object in this namespace."""
        kind = manifest["kind"]
        if self._in_cluster():
            prefix, plural = self._PLURAL[kind]
            return self._api(
                "POST", f"/{prefix}/namespaces/{self.namespace}/{plural}",
                manifest)
        self._kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
        return manifest

    def delete(self, kind: str, name: str) -> bool:
        """DELETE the object; False when it is already gone (404)."""
        import urllib.error

        if self._in_cluster():
            prefix, plural = self._PLURAL[kind]
            try:
                self._api("DELETE",
                          f"/{prefix}/namespaces/{self.namespace}/{plural}/{name}")
                return True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return False
                raise
        self._kubectl("delete", kind.lower(), name, "--ignore-not-found=true")
        return True

    def list_names(self, kind: str, label_selector: str) -> list[str]:
        """Object names matching a label selector (e.g. ``station-id=7``) —
        how the panel re-resolves a station's Service by its row id (names
        carry a random suffix, labels do not)."""
        if self._in_cluster():
            prefix, plural = self._PLURAL[kind]
            import urllib.parse

            q = urllib.parse.quote(label_selector)
            out = self._api(
                "GET",
                f"/{prefix}/namespaces/{self.namespace}/{plural}"
                f"?labelSelector={q}")
            return [item["metadata"]["name"] for item in out.get("items", [])]
        raw = self._kubectl("get", kind.lower(), "-l", label_selector,
                            "-o", "jsonpath={.items[*].metadata.name}")
        return [n for n in raw.split() if n]


_client: StationK8sClient | None = None


def get_station_client() -> StationK8sClient:
    """Process-wide station client (tests monkeypatch this seam)."""
    global _client
    if _client is None:
        _client = StationK8sClient()
    return _client


# ── manifests ────────────────────────────────────────────────────────────────
def _dnsify(value: str) -> str:
    """k8s-name-safe: lowercase, non-alphanumerics collapse to '-' (RFC 1123
    label; underscores are invalid in object names)."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "x"


def station_name(source: str, account_alias: str) -> str:
    """``login-<src>-<alias>-<rand6>`` — shared by the Job and its Service."""
    return f"login-{_dnsify(source)}-{_dnsify(account_alias)}-{secrets.token_hex(3)}"


def _labels(station_id: int, source: str, identity_id: int) -> dict:
    # label values: k8s requires <=63 chars, alphanumerics + -_. — sources fit
    return {"app": "login-station", "station-id": str(station_id),
            "source": source, "identity-id": str(identity_id)}


def build_station_manifests(
    *, station_id: int, source: str, account_alias: str, identity_id: int,
    proxy_url: str | None, image: str | None = None,
    namespace: str = STATION_NAMESPACE,
) -> tuple[dict, dict]:
    """The (Job, Service) pair for one station launch.

    Job: image + ``python3 scripts/login_station.py --station <src> <alias>``,
    backoffLimit 0 (a failed login is an event, not a retry), deadline
    ``station_deadline_seconds()`` (60 min by default — the human needs time to
    reach the observation view and complete a phone+password login);
    initContainer runs the dispatcher.yaml git sparse-checkout (spiders/ into
    /content, FD_CONTENT_DIR=/content/spiders); env = the four cluster secrets
    + STATION_ID + FD_SCHEMA_MANAGED=1 + optional PROXY_URL + PYTHONUNBUFFERED.
    Service: ClusterIP 6080 -> 6080, selector = the pod labels."""
    image = image or station_image()
    labels = _labels(station_id, source, identity_id)
    name = station_name(source, account_alias)
    deadline_seconds = station_deadline_seconds()
    env = [dict(e) for e in STATION_SECRET_ENV] + [
        {"name": "STATION_ID", "value": str(station_id)},
        {"name": "FD_STATION_DEADLINE_SECONDS",
         "value": str(deadline_seconds)},
        {"name": "FD_SCHEMA_MANAGED", "value": "1"},
        {"name": "FD_CONTENT_DIR", "value": "/content/spiders"},
        {"name": "PYTHONUNBUFFERED", "value": "1"},
    ]
    if proxy_url:
        env.append({"name": "PROXY_URL", "value": proxy_url})
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "initContainers": [{
                        "name": "checkout",
                        "image": image,
                        "env": [{"name": "GITEE_TOKEN",
                                 "valueFrom": {"secretKeyRef": {
                                     "name": "fd-industry-gitee-token",
                                     "key": "token"}}}],
                        "command": ["sh", "-c", STATION_CHECKOUT_SCRIPT],
                        "volumeMounts": [{"name": "content",
                                          "mountPath": "/content"}],
                    }],
                    "containers": [{
                        "name": "station",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["python3", "scripts/login_station.py",
                                    "--station", source, account_alias],
                        "workingDir": "/content",
                        "env": env,
                        "ports": [{"containerPort": STATION_PORT,
                                   "name": "vnc"}],
                        "volumeMounts": [{"name": "content",
                                          "mountPath": "/content"}],
                        "resources": {
                            "requests": {"memory": "512Mi", "cpu": "250m"},
                            "limits": {"memory": "2Gi", "cpu": "1000m"},
                        },
                    }],
                    "volumes": [{"name": "content", "emptyDir": {}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "type": "ClusterIP",
            "selector": labels,   # matches the pod template labels above
            "ports": [{"name": "vnc", "port": STATION_PORT,
                       "targetPort": STATION_PORT}],
        },
    }
    return job, service


# ── lifecycle ────────────────────────────────────────────────────────────────
def create_station(
    session: Session,
    source: str,
    account_alias: str,
    client: StationK8sClient | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Launch a login station for an identity: insert the
    ``crawl_login_stations`` row (launching, deadline
    now + ``station_deadline_seconds()`` — 60 min by default), create the
    Job + Service, then mark the row waiting_operator (the runtime's first
    report flips it onward). A launch failure marks the row failed and
    returns the reason — never a bare exception to the panel."""
    now = _as_aware(now) or _utcnow()
    client = client or get_station_client()
    ident = (session.query(CrawlIdentity)
             .filter_by(source=source, account_alias=account_alias).first())
    if ident is None:
        return {"status": "not_found",
                "reason": (f"no identity {source}/{account_alias} — register "
                           f"it first (ensure_identity_with_egress)")}
    proxy_url = proxy_url_for_ref(session, ident.egress_ref)
    station = CrawlLoginStation(
        identity_id=ident.id, source=source, account_alias=account_alias,
        status="launching", proxy_url=proxy_url, created_at=now,
        deadline_at=now + dt.timedelta(seconds=station_deadline_seconds()))
    session.add(station)
    session.flush()  # station.id -> STATION_ID env + labels
    job, service = build_station_manifests(
        station_id=station.id, source=source, account_alias=account_alias,
        identity_id=ident.id, proxy_url=proxy_url, namespace=client.namespace)
    try:
        client.create(job)
        client.create(service)
    except Exception as e:  # noqa: BLE001 - launch failure is a station event
        logger.exception("station launch failed for %s/%s", source, account_alias)
        station.status = "failed"
        station.finished_at = now
        station.note = f"launch failed: {e}"
        session.add(CrawlIdentityEvent(
            identity_id=ident.id, kind="note",
            detail=f"login station #{station.id} launch failed: {e}",
            created_at=now))
        session.commit()
        return {"status": "failed", "station_id": station.id,
                "reason": f"launch failed: {e}",
                "vnc_path": station_vnc_path(station.id)}
    station.status = "waiting_operator"
    station.note = f"job={job['metadata']['name']} ns={client.namespace}"
    session.add(CrawlIdentityEvent(
        identity_id=ident.id, kind="note",
        detail=(f"login station #{station.id} launched for {source}/"
                f"{account_alias} (job {job['metadata']['name']})"),
        created_at=now))
    session.commit()
    logger.info("login station %d launched for %s/%s (job %s)",
                station.id, source, account_alias, job["metadata"]["name"])
    return {"status": "launched", "station_id": station.id,
            "identity_id": ident.id, "source": source,
            "account_alias": account_alias,
            "job_name": job["metadata"]["name"],
            "service_name": service["metadata"]["name"],
            "egress_ref": ident.egress_ref,
            "deadline_at": _iso(station.deadline_at),
            "vnc_path": station_vnc_path(station.id)}


def station_vnc_path(station_id: int) -> str:
    """Relative panel URL of the embedded noVNC view (no token ever embedded —
    the channel sits behind the panel gate; the caller authenticates)."""
    return (f"/panel/auth/station/{station_id}/vnc/vnc.html"
            f"?autoconnect=1&path=websockify")


def _apply_timeouts(session: Session, now: dt.datetime) -> int:
    """Deadline backstop: every non-terminal station whose deadline passed
    closes as timeout (identity stays login_required; the timeout is evented
    — spec: 站自动回收, 留痕超时事件). Python-side comparison: the row count is
    tiny and sqlite/PG timestamp aware-ness differs."""
    closed = 0
    for st in (session.query(CrawlLoginStation)
               .filter(CrawlLoginStation.status.in_(STATION_OPEN)).all()):
        deadline = _as_aware(st.deadline_at)
        if deadline is None or deadline >= now:
            continue
        st.status = "timeout"
        st.finished_at = now
        st.note = ((st.note or "") + " | timed out: deadline passed").strip(" |")
        session.add(CrawlIdentityEvent(
            identity_id=st.identity_id, kind="note",
            detail=(f"login station #{st.id} timed out at deadline "
                    f"(identity stays login_required)"),
            created_at=now))
        closed += 1
    if closed:
        session.commit()
    return closed


def station_status(
    session: Session,
    now: dt.datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Station rows newest-first (display projection; proxy credentials
    masked), applying the deadline-timeout backstop on every read."""
    now = _as_aware(now) or _utcnow()
    _apply_timeouts(session, now)
    rows = (session.query(CrawlLoginStation)
            .order_by(CrawlLoginStation.id.desc()).limit(limit).all())
    out = []
    for st in rows:
        label, url = source_label(st.source)
        out.append({
            "id": st.id, "identity_id": st.identity_id, "source": st.source,
            "source_label": label, "source_url": url,
            "account_alias": st.account_alias, "status": st.status,
            "proxy_url_masked": mask_proxy_url(st.proxy_url),
            "note": st.note, "created_at": _iso(st.created_at),
            "deadline_at": _iso(st.deadline_at), "finished_at": _iso(st.finished_at),
            "live": st.status in STATION_OPEN,
            "error": station_error(st),
        })
    return out


def station_brief(session: Session, station_id: int,
                  now: dt.datetime | None = None) -> dict | None:
    """One station's polling payload (modal status refresh): identity context,
    display label and live/error state — None for unknown stations."""
    for st in station_status(session, now=now):
        if st["id"] == station_id:
            return st
    return None


def station_error(st) -> str | None:
    """The operator-facing reason a station ended badly (None when it did not).

    Station failures were invisible on the board: the runtime writes the
    traceback into ``note`` while the panel showed only a badge, so an
    operator saw 'failed' with no idea whether to retry or fix something.
    The first meaningful line of the terminal note is the fix-worthy signal —
    Playwright's 'Call log:' block below it is noise for this purpose.

    Credentials are scrubbed before the line is ever displayed: a Playwright
    error quotes the proxy URL it dialled, user:pass@host and all.
    """
    if st.status not in ("failed", "timeout"):
        return None
    lines = [ln.strip() for ln in (st.note or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("Call log:"):
            break
        if ln.startswith(("Error:", "TimeoutError", "RuntimeError",
                          "Playwright", "Exception", "Traceback")):
            return mask_proxy_url_in_text(ln)
    return mask_proxy_url_in_text(lines[0]) if lines else None


def mask_proxy_url_in_text(text: str) -> str:
    """Strip ``user:pass@`` from any URL embedded in free text (a panel body
    must never carry egress credentials, even quoted inside an error)."""
    return re.sub(r"(https?://)[^@/\s]+@", r"\1••••@", text)


def active_stations_summary(session: Session,
                            now: dt.datetime | None = None) -> dict:
    """The ``auth_status`` stations payload: how many stations are in flight
    (launching / waiting_operator), after the timeout backstop."""
    now = _as_aware(now) or _utcnow()
    _apply_timeouts(session, now)
    open_rows = (session.query(CrawlLoginStation)
                 .filter(CrawlLoginStation.status.in_(STATION_OPEN)).count())
    return {"active": int(open_rows or 0)}


# ── 预拉起 pre-arm (change prearm-login-station) ─────────────────────────────
# The nightly rmfyalk run failed 'auth pool dry' because the schedule fired at
# Beijing 03:10 with a session that had expired hours earlier and nobody awake
# to re-login (2026-10-09). The fix: the Console watches the clock and opens
# the login station BEFORE the fire, so the operator finds the window already
# open — or, if they are asleep, the window at least spans the fire.
def _source_next_fire(src, now: dt.datetime) -> dt.datetime | None:
    """The source's next fire as an aware UTC datetime, or None when its cron
    cannot be parsed. The cron is matched in the source's own ``schedule_tz``
    (NULL = UTC; unknown tz degrades to UTC with a warning, the dispatcher's
    ``_schedule_now`` convention) — the same semantics the dispatcher uses to
    enqueue, so pre-arm and execution can never disagree about "due"."""
    try:
        tz = ZoneInfo(src.schedule_tz) if src.schedule_tz else dt.timezone.utc
    except Exception:  # noqa: BLE001 - unknown tz degrades to UTC, never raises
        logger.warning("source %s: unknown schedule_tz %r; treating schedule "
                       "as UTC", src.source, src.schedule_tz)
        tz = dt.timezone.utc
    try:
        local = croniter(src.schedule, now.astimezone(tz)).get_next(dt.datetime)
    except Exception as e:  # noqa: BLE001 - a bad cron must not break the tick
        logger.warning("source %s: schedule %r cron parse failed: %s",
                       src.source, src.schedule, e)
        return None
    return local.astimezone(dt.timezone.utc)


def _runnable_source(src) -> bool:
    """A source the dispatcher would actually execute: platform rows need a
    manifest mirror (last_commit), federated rows a runner command. Anything
    else has no runner to pre-arm for."""
    if src.kind == "federated":
        return src.runner_command is not None
    if src.kind == "platform":
        return src.last_commit is not None
    return False


def _fresh_identity(session: Session, profile: str,
                    now: dt.datetime) -> bool:
    """True while the profile has any identity proven live recently: status
    active AND probed within the freshness window. A probe is the login
    unit's own trust step (auth.complete_login writes last_probe_at), so this
    is the same fact the pool uses to hand out leases."""
    cutoff = now - dt.timedelta(hours=prearm_fresh_hours())
    for ident in (session.query(CrawlIdentity)
                  .filter(CrawlIdentity.source == profile).all()):
        if ident.status != "active":
            continue
        probed = _as_aware(ident.last_probe_at)
        if probed is not None and probed >= cutoff:
            return True
    return False


def prearm_plan(session: Session,
                now: dt.datetime | None = None) -> list[dict]:
    """Which scheduled authenticated sources need a login station opened now.

    Pure read (no writes, no cluster) so the decision is unit-testable: for
    every enabled, runnable ``crawl_sources`` row carrying an ``auth_profile``
    whose next fire is inside the FD_PREARM_MINUTES window (default 45) and
    whose profile has no fresh identity (status active, probed within
    FD_PREARM_FRESH_HOURS, default 6), emit one item naming the alias to log
    in. Skips when an open station already exists for the source (the
    operator is presumably on it) and when the profile has no identity rows at
    all (registration stays a manual act).

    Returns a list of ``{"source", "account_alias", "next_fire", "minutes",
    "reason"}``.
    """
    now = _as_aware(now) or _utcnow()
    window = dt.timedelta(minutes=prearm_minutes())
    out: list[dict] = []
    planned: set[str] = set()   # one station per profile: several sources may
    #                             share one login unit (auth_profile)
    rows = (session.query(CrawlSource)
            .filter(CrawlSource.enabled.is_(True),
                    CrawlSource.auth_profile.isnot(None),
                    CrawlSource.schedule.isnot(None))
            .order_by(CrawlSource.source).all())
    for src in rows:
        if not _runnable_source(src):
            continue
        profile = src.auth_profile
        if profile in planned:
            continue
        next_fire = _source_next_fire(src, now)
        if next_fire is None:
            continue
        minutes = (next_fire - now).total_seconds() / 60.0
        if minutes > window.total_seconds() / 60.0:
            continue
        if _fresh_identity(session, profile, now):
            continue
        # An open station means the operator is presumably on it — and this is
        # what stops the 5-min loop from stacking duplicates across the whole
        # pre-arm window. Both spellings are checked because both are in use
        # for one login unit: pre-arm/dispatcher key identities by
        # auth_profile, while the panel's manual form posts the crawl source
        # name. A crawl source has one profile, so nothing else is suppressed.
        open_station = (session.query(CrawlLoginStation)
                        .filter(CrawlLoginStation.status.in_(STATION_OPEN),
                                CrawlLoginStation.source.in_(
                                    (profile, src.source)))
                        .first())
        if open_station is not None:
            continue
        identities = (session.query(CrawlIdentity)
                      .filter(CrawlIdentity.source == profile)
                      .order_by(CrawlIdentity.id).all())
        if not identities:
            continue  # no account yet: registration is a deliberate human act
        planned.add(profile)
        # most recently logged-in account first; never-logged-in ties break on
        # the highest id (the newest registration)
        alias = max(identities,
                    key=lambda i: (_as_aware(i.last_login_at) or
                                   dt.datetime.min.replace(
                                       tzinfo=dt.timezone.utc),
                                   i.id)).account_alias
        out.append({"source": profile, "account_alias": alias,
                    "next_fire": _iso(next_fire), "minutes": int(minutes),
                    "reason": "stale or missing identity"})
    return out


def prearm_tick(session: Session, now: dt.datetime | None = None,
                client: StationK8sClient | None = None) -> list[dict]:
    """Execute one pre-arm pass: for every ``prearm_plan`` item, ensure the
    identity (+ egress) and open a login station.

    Each source is independent — one failure is logged and skipped, never
    aborting the others (the caller is a background loop). Returns the
    launched stations as ``{"source", "account_alias", "station_id",
    "status", ...}``; a non-launched outcome is logged with its reason and
    still returned (the operator-facing board reads the row either way).
    """
    now = _as_aware(now) or _utcnow()
    client = client or get_station_client()
    launched: list[dict] = []
    for item in prearm_plan(session, now=now):
        source, alias = item["source"], item["account_alias"]
        try:
            ensured = ensure_identity_with_egress(
                session, source, alias, requested_by="prearm", now=now)
            if ensured.get("status") != "queued":
                logger.warning("prearm: %s/%s ensure refused: %s",
                               source, alias, ensured.get("reason"))
                continue
            out = create_station(session, source, alias, client=client,
                                 now=now)
            if out.get("status") != "launched":
                logger.warning("prearm: %s/%s station not launched: %s",
                               source, alias, out.get("reason"))
            else:
                logger.info(
                    "prearm: station #%s launched for %s/%s "
                    "(next fire %s, %s min away)",
                    out["station_id"], source, alias,
                    item["next_fire"], item["minutes"])
            launched.append({**item, **out})
        except Exception:  # noqa: BLE001 - one source must not stop the pass
            logger.exception("prearm: %s/%s failed", source, alias)
    return launched


def auth_alert_summary(session: Session,
                       now: dt.datetime | None = None) -> dict:
    """The /panel/auth standing banner counts.

    ``login_required`` = identities waiting for a login; ``stale`` = identities
    marked active whose last probe is missing or older than
    FD_PREARM_FRESH_HOURS (the session almost certainly expired — the exact
    condition that produced the 2026-10-09 'auth pool dry'); ``total`` = every
    identity row. A standing banner, not a toast: the operator must see it
    without having watched the moment it appeared.
    """
    now = _as_aware(now) or _utcnow()
    cutoff = now - dt.timedelta(hours=prearm_fresh_hours())
    login_required = stale = total = 0
    for ident in session.query(CrawlIdentity).all():
        total += 1
        if ident.status == "login_required":
            login_required += 1
        elif ident.status == "active":
            probed = _as_aware(ident.last_probe_at)
            if probed is None or probed < cutoff:
                stale += 1
    return {"login_required": login_required, "stale": stale,
            "total": total}


def reclaim_station(
    session: Session,
    station_id: int,
    client: StationK8sClient | None = None,
    now: dt.datetime | None = None,
    actor: str = "panel",
) -> dict:
    """Tear a station down: delete its Job + Service (resolved by the
    station-id label), close a non-terminal row as reclaimed. A terminal row's
    status is history and stays put — only the cluster objects are cleaned."""
    now = _as_aware(now) or _utcnow()
    client = client or get_station_client()
    st = session.get(CrawlLoginStation, station_id)
    if st is None:
        return {"status": "not_found",
                "reason": f"station {station_id} not found"}
    deleted: list[str] = []
    for kind in ("Job", "Service"):
        for name in client.list_names(kind, f"station-id={station_id}"):
            try:
                client.delete(kind, name)
                deleted.append(name)
            except Exception as e:  # noqa: BLE001 - teardown is best-effort
                logger.warning("station %s: delete %s %s failed: %s",
                               station_id, kind, name, e)
    if st.status in STATION_TERMINAL:
        session.commit()
        return {"status": "already_finished", "station_id": station_id,
                "current_status": st.status, "deleted": deleted}
    st.status = "reclaimed"
    st.finished_at = now
    st.note = ((st.note or "") + f" | reclaimed by {actor}").strip(" |")
    session.add(CrawlIdentityEvent(
        identity_id=st.identity_id, kind="note",
        detail=(f"login station #{station_id} reclaimed by {actor} "
                f"(identity stays login_required)"),
        created_at=now))
    session.commit()
    return {"status": "reclaimed", "station_id": station_id, "deleted": deleted}
