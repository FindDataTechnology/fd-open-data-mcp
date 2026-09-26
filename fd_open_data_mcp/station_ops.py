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

from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    CrawlIdentity, CrawlIdentityEvent, CrawlLoginStation, Proxy,
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
STATION_DEADLINE_SECONDS = 1500     # 25 min: row deadline, Job activeDeadline and runtime backstop
STATION_IMAGE_DEFAULT = "fd-industry-runner:latest"  # placeholder when STATION_IMAGE is unset
STATION_TERMINAL = ("completed", "failed", "timeout", "reclaimed")
STATION_OPEN = ("launching", "waiting_operator")

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
) -> dict:
    """Register/reset an identity to ``login_required`` and make sure it has
    an egress binding — the one write every identity-creation entrance funnels
    through (panel form, MCP tool, station launch).

    Composition: auth_tools.request_identity_login does the row write (reset
    semantics + audit event); the egress step mirrors assign_egress
    (idempotent: an already-bound identity keeps its proxy). Returns the
    request_identity_login dict plus ``egress_ref`` / ``proxy_url``."""
    from fd_open_data_mcp.auth_tools import request_identity_login  # lazy: auth_tools imports this module

    now = _as_aware(now) or _utcnow()
    out = request_identity_login(session, source, account_alias,
                                 automation=automation,
                                 requested_by=requested_by, now=now)
    if out.get("status") != "queued":
        return {**out, "egress_ref": None, "proxy_url": None}
    ident = session.get(CrawlIdentity, out["identity_id"])
    assert ident is not None  # request_identity_login just committed it
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
    backoffLimit 0 (a failed login is an event, not a retry), deadline 1500s;
    initContainer runs the dispatcher.yaml git sparse-checkout (spiders/ into
    /content, FD_CONTENT_DIR=/content/spiders); env = the four cluster secrets
    + STATION_ID + FD_SCHEMA_MANAGED=1 + optional PROXY_URL + PYTHONUNBUFFERED.
    Service: ClusterIP 6080 -> 6080, selector = the pod labels."""
    image = image or station_image()
    labels = _labels(station_id, source, identity_id)
    name = station_name(source, account_alias)
    env = [dict(e) for e in STATION_SECRET_ENV] + [
        {"name": "STATION_ID", "value": str(station_id)},
        {"name": "FD_STATION_DEADLINE_SECONDS",
         "value": str(STATION_DEADLINE_SECONDS)},
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
            "activeDeadlineSeconds": STATION_DEADLINE_SECONDS,
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
    ``crawl_login_stations`` row (launching, deadline now+25min), create the
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
        deadline_at=now + dt.timedelta(seconds=STATION_DEADLINE_SECONDS))
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
    return [{
        "id": st.id, "identity_id": st.identity_id, "source": st.source,
        "account_alias": st.account_alias, "status": st.status,
        "proxy_url_masked": mask_proxy_url(st.proxy_url),
        "note": st.note, "created_at": _iso(st.created_at),
        "deadline_at": _iso(st.deadline_at), "finished_at": _iso(st.finished_at),
        "live": st.status in STATION_OPEN,
    } for st in rows]


def active_stations_summary(session: Session,
                            now: dt.datetime | None = None) -> dict:
    """The ``auth_status`` stations payload: how many stations are in flight
    (launching / waiting_operator), after the timeout backstop."""
    now = _as_aware(now) or _utcnow()
    _apply_timeouts(session, now)
    open_rows = (session.query(CrawlLoginStation)
                 .filter(CrawlLoginStation.status.in_(STATION_OPEN)).count())
    return {"active": int(open_rows or 0)}


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
