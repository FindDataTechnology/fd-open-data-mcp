"""Login-station orchestration tests (login-station-console 3.1).

Covers station_ops on the per-test sqlite fixture: the egress allocator
(distinct egress per account, healthy-only, same-source exclusion, least-used,
idempotent rebind), the station row state machine (launching ->
waiting_operator -> timeout backstop / reclaim), and the Job/Service manifests
against a FakeStationClient — the injectable boundary at which tests stop: no
k8s API, no pod, nothing really launched.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from fd_open_data_mcp import station_ops
from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    CrawlIdentity, CrawlIdentityEvent, CrawlLoginStation, CrawlSite,
    CrawlSource, Proxy,
)

NOW = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=dt.timezone.utc)


# ── the fake k8s client (shared with test_panel_station) ─────────────────────
class FakeStationClient:
    """Test double for StationK8sClient: records created manifests, resolves
    object names by label (the real client's labelSelector list), delete()
    removes them. This is where the launch path under test ends — no cluster,
    no pod. ``fail_create=True`` simulates an unreachable API server."""

    def __init__(self, namespace=station_ops.STATION_NAMESPACE,
                 fail_create=False):
        self.namespace = namespace
        self.created: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_create = fail_create

    def create(self, manifest: dict) -> dict:
        if self.fail_create:
            raise RuntimeError("api server unreachable")
        self.created.append(json.loads(json.dumps(manifest)))
        return manifest

    def delete(self, kind: str, name: str) -> bool:
        before = len(self.created)
        self.created = [m for m in self.created
                        if not (m["kind"] == kind
                                and m["metadata"]["name"] == name)]
        self.deleted.append((kind, name))
        return len(self.created) < before

    def list_names(self, kind: str, label_selector: str) -> list[str]:
        key, _, val = label_selector.partition("=")
        return [m["metadata"]["name"] for m in self.created
                if m["kind"] == kind
                and str(m["metadata"]["labels"].get(key)) == val]

    def manifests(self, kind: str) -> list[dict]:
        return [m for m in self.created if m["kind"] == kind]


# ── seed helpers ────────────────────────────────────────────────────────────
def _source(name, site="tencent", auth_profile=None) -> str:
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, site) is None:
            s.add(CrawlSite(id=site, enabled=True))
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(source=name, site=site, schedule="0 6 * * *",
                              enabled=True, auth_profile=auth_profile))
        s.commit()
        return name
    finally:
        s.close()


def _proxy(ip="10.0.0.1", port=8080, scheme="http", auth="u:p", **kw) -> int:
    s = get_database().get_session()
    try:
        row = Proxy(scheme=scheme, ip=ip, port=port, auth=auth, **kw)
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _ident(source, alias, status="active", automation="assisted", **kw) -> int:
    s = get_database().get_session()
    try:
        row = CrawlIdentity(source=source, account_alias=alias, status=status,
                            automation=automation, **kw)
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _station(identity_id, source, alias, status="waiting_operator",
             deadline=None, proxy_url=None, **kw) -> int:
    s = get_database().get_session()
    try:
        row = CrawlLoginStation(identity_id=identity_id, source=source,
                                account_alias=alias, status=status,
                                deadline_at=deadline, proxy_url=proxy_url,
                                created_at=NOW, **kw)
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _events(identity_id) -> list[str]:
    s = get_database().get_session()
    try:
        return [e.detail for e in s.query(CrawlIdentityEvent)
                .filter_by(identity_id=identity_id)
                .order_by(CrawlIdentityEvent.id).all()]
    finally:
        s.close()


# ── ensure_identity_with_egress: the allocator ──────────────────────────────
def test_two_identities_get_distinct_egress(session):
    p1 = _proxy(ip="10.0.0.1")
    p2 = _proxy(ip="10.0.0.2")

    a = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-a", now=NOW)
    b = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-b", now=NOW)

    # distinct egress (id-asc first, then the other — never the same one for
    # two accounts of one source)
    assert a["egress_ref"] == f"proxy:{p1}"
    assert b["egress_ref"] == f"proxy:{p2}"
    assert a["proxy_url"] == "http://u:p@10.0.0.1:8080"
    assert b["proxy_url"] == "http://u:p@10.0.0.2:8080"
    # both identities queued login_required with their binding recorded
    assert a["status"] == "queued" and b["status"] == "queued"
    # the assignment is evented
    assert "egress assigned proxy:%d" % p1 in _events(a["identity_id"])


def test_ensure_resets_to_login_required_and_keeps_binding(session):
    p1 = _proxy(ip="10.0.0.1")
    iid = _ident("rmfyalk", "acc-live", "active",
                 lease_owner="disp-1", lease_token="tok-99",
                 lease_expires_at=NOW + dt.timedelta(hours=1))
    s = get_database().get_session()
    try:
        row = s.get(CrawlIdentity, iid)
        row.egress_ref = f"proxy:{p1}"
        s.commit()
    finally:
        s.close()

    out = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-live", now=NOW)

    # reset semantics: login_required + lease cleared, binding KEPT
    assert out["created"] is False and out["previous_status"] == "active"
    assert out["egress_ref"] == f"proxy:{p1}"
    s = get_database().get_session()
    try:
        row = s.get(CrawlIdentity, iid)
        s.refresh(row)
        assert row.status == "login_required"
        assert row.lease_owner is None and row.lease_expires_at is None
        assert row.egress_ref == f"proxy:{p1}"
    finally:
        s.close()
    # idempotent: no second assignment event, only the login-request note
    assert _events(iid).count("egress assigned proxy:%d" % p1) == 0


def test_allocation_skips_unhealthy_and_shares_cross_source(session):
    _proxy(scheme="direct", ip="direct", port=None)               # not an egress
    _proxy(ip="10.9.9.9", retired_at=NOW - dt.timedelta(days=1))  # retired
    _proxy(ip="10.9.9.8", status="retired")                       # retired (status)
    p3 = _proxy(ip="10.0.0.3")

    a = station_ops.ensure_identity_with_egress(session, "src-a", "acc-1", now=NOW)
    assert a["egress_ref"] == f"proxy:{p3}"

    # same source, only p3 exists and it is taken -> pool dry, evented
    b = station_ops.ensure_identity_with_egress(session, "src-a", "acc-2", now=NOW)
    assert b["egress_ref"] is None and b["proxy_url"] is None
    assert "pool dry" in _events(b["identity_id"])[-1]

    # cross-source sharing is allowed (same-source exclusion only)
    c = station_ops.ensure_identity_with_egress(session, "src-b", "acc-1", now=NOW)
    assert c["egress_ref"] == f"proxy:{p3}"


def test_allocation_prefers_least_referenced(session):
    # p1 already referenced by an unrelated source's identity; p2 is free
    p1 = _proxy(ip="10.0.0.1")
    p2 = _proxy(ip="10.0.0.2")
    iid = _ident("other", "acc-x", "active", egress_ref=f"proxy:{p1}")
    out = station_ops.ensure_identity_with_egress(session, "src-a", "acc-1", now=NOW)
    assert out["egress_ref"] == f"proxy:{p2}"


# ── proxy_url_for_ref ────────────────────────────────────────────────────────
def test_proxy_url_for_ref_resolution(session):
    p = _proxy(ip="10.1.1.1", port=3128, auth="user:pw")
    s = get_database().get_session()
    try:
        assert station_ops.proxy_url_for_ref(s, f"proxy:{p}") == \
            "http://user:pw@10.1.1.1:3128"
        for bad in (None, "", "proxy:notanumber", "proxy:999999",
                    "direct", "http://x"):
            assert station_ops.proxy_url_for_ref(s, bad) is None
    finally:
        s.close()
    # retired / direct resolve to None
    pr = _proxy(ip="10.1.1.2", retired_at=NOW)
    pd = _proxy(scheme="direct", ip="direct", port=None)
    s = get_database().get_session()
    try:
        assert station_ops.proxy_url_for_ref(s, f"proxy:{pr}") is None
        assert station_ops.proxy_url_for_ref(s, f"proxy:{pd}") is None
        assert station_ops.mask_proxy_url("http://u:sekret@1.2.3.4:80") == \
            "http://••••@1.2.3.4:80"
        assert station_ops.mask_proxy_url(None) == "—"
    finally:
        s.close()


# ── create_station: row state machine + Job/Service spec ────────────────────
def test_create_station_job_and_service_spec(session):
    p = _proxy(ip="10.0.0.7", port=3128, auth="user:pw")
    _source("rmfy-alk")
    station_ops.ensure_identity_with_egress(
        session, "rmfy-alk", "acc 1!", now=NOW)
    fake = FakeStationClient()

    out = station_ops.create_station(session, "rmfy-alk", "acc 1!",
                                     client=fake, now=NOW)

    assert out["status"] == "launched"
    sid = out["station_id"]
    job = fake.manifests("Job")[0]
    service = fake.manifests("Service")[0]

    # naming: login-<src '-'-sanitized>-<alias sanitized>-<rand6>, shared by both
    assert re.fullmatch(r"login-rmfy-alk-acc-1-[0-9a-f]{6}",
                        job["metadata"]["name"])
    assert service["metadata"]["name"] == job["metadata"]["name"]
    assert job["metadata"]["namespace"] == station_ops.STATION_NAMESPACE
    assert service["metadata"]["namespace"] == station_ops.STATION_NAMESPACE

    js = job["spec"]
    assert js["backoffLimit"] == 0                       # failure is an event, not a retry
    assert js["activeDeadlineSeconds"] == 1500           # the hard deadline
    assert js["template"]["spec"]["restartPolicy"] == "Never"

    # the runtime contract: login_station.py --station <src> <alias> in /content
    c = js["template"]["spec"]["containers"][0]
    assert c["command"] == ["python3", "scripts/login_station.py",
                            "--station", "rmfy-alk", "acc 1!"]
    assert c["workingDir"] == "/content"

    # initContainer: the dispatcher.yaml git sparse-checkout pattern
    init = js["template"]["spec"]["initContainers"][0]
    script = init["command"][-1]
    assert "git clone --depth 1 --filter=blob:none --sparse" in script
    assert "git sparse-checkout set spiders/" in script
    assert init["env"][0] == {
        "name": "GITEE_TOKEN",
        "valueFrom": {"secretKeyRef": {"name": "fd-industry-gitee-token",
                                       "key": "token"}}}

    # env: the four cluster secrets + the station contract vars + egress
    env = {e["name"]: e for e in c["env"]}
    assert env["FD_CRAWL_DB_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "fd-industry-db", "key": "url"}
    assert env["RUSTFS_ENDPOINT"]["valueFrom"]["secretKeyRef"]["name"] == \
        "fd-industry-rustfs"
    assert env["RUSTFS_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == \
        "fd-industry-rustfs"
    assert env["RUSTFS_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == \
        "fd-industry-rustfs"
    assert env["PLATFORM_SESSION_KEY"]["valueFrom"]["secretKeyRef"]["name"] == \
        "platform-session-key"
    assert env["STATION_ID"]["value"] == str(sid)
    assert env["FD_STATION_DEADLINE_SECONDS"]["value"] == "1500"
    assert env["FD_SCHEMA_MANAGED"]["value"] == "1"
    assert env["FD_CONTENT_DIR"]["value"] == "/content/spiders"
    assert env["PROXY_URL"]["value"] == "http://user:pw@10.0.0.7:3128"
    assert env["PYTHONUNBUFFERED"]["value"] == "1"

    # Service: ClusterIP 6080->6080, selector == the pod labels
    labels = js["template"]["metadata"]["labels"]
    assert labels["station-id"] == str(sid)
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == labels
    assert service["spec"]["ports"] == [{"name": "vnc", "port": 6080,
                                         "targetPort": 6080}]

    # the row: launching -> waiting_operator, deadline = now + 25min, egress recorded
    s = get_database().get_session()
    try:
        row = s.get(CrawlLoginStation, sid)
        assert row.status == "waiting_operator"
        assert station_ops._as_aware(row.deadline_at) == \
            NOW + dt.timedelta(seconds=1500)
        assert row.proxy_url == "http://user:pw@10.0.0.7:3128"
    finally:
        s.close()
    # launch is evented on the identity
    assert any(f"login station #{sid} launched" in d
               for d in _events(out["identity_id"]))
    # the panel view path is relative (no credential embedded)
    assert out["vnc_path"] == (f"/panel/auth/station/{sid}/vnc/vnc.html"
                               f"?autoconnect=1&path=websockify")


def test_create_station_without_egress_omits_proxy(session):
    _source("rmfyalk")
    station_ops.ensure_identity_with_egress(session, "rmfyalk", "acc-free",
                                            now=NOW)  # pool empty -> unbound
    fake = FakeStationClient()
    out = station_ops.create_station(session, "rmfyalk", "acc-free",
                                     client=fake, now=NOW)
    assert out["status"] == "launched"
    c = fake.manifests("Job")[0]["spec"]["template"]["spec"]["containers"][0]
    env_names = {e["name"] for e in c["env"]}
    assert "PROXY_URL" not in env_names
    s = get_database().get_session()
    try:
        assert s.get(CrawlLoginStation, out["station_id"]).proxy_url is None
    finally:
        s.close()


def test_station_image_env_override(session, monkeypatch):
    _source("rmfyalk")
    station_ops.ensure_identity_with_egress(session, "rmfyalk", "acc-img",
                                            now=NOW)
    fake = FakeStationClient()
    monkeypatch.setenv("STATION_IMAGE",
                       "reg.example.com/finddata/fd-industry-runner:sha-abc")
    station_ops.create_station(session, "rmfyalk", "acc-img", client=fake,
                               now=NOW)
    job = fake.manifests("Job")[0]
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == \
        "reg.example.com/finddata/fd-industry-runner:sha-abc"
    assert job["spec"]["template"]["spec"]["initContainers"][0]["image"] == \
        "reg.example.com/finddata/fd-industry-runner:sha-abc"
    # empty/unset degrades to the placeholder tag
    monkeypatch.setenv("STATION_IMAGE", "")
    assert station_ops.station_image() == station_ops.STATION_IMAGE_DEFAULT
    monkeypatch.delenv("STATION_IMAGE", raising=False)
    assert station_ops.station_image() == "fd-industry-runner:latest"


def test_create_station_launch_failure_marks_row_failed(session):
    _source("rmfyalk")
    out = station_ops.ensure_identity_with_egress(session, "rmfyalk", "acc-f",
                                                  now=NOW)
    fake = FakeStationClient(fail_create=True)
    res = station_ops.create_station(session, "rmfyalk", "acc-f",
                                     client=fake, now=NOW)
    assert res["status"] == "failed"
    assert "unreachable" in res["reason"]
    s = get_database().get_session()
    try:
        row = s.get(CrawlLoginStation, res["station_id"])
        assert row.status == "failed"
        assert station_ops._as_aware(row.finished_at) == NOW
        assert "launch failed" in row.note
    finally:
        s.close()
    assert any("launch failed" in d for d in _events(out["identity_id"]))


def test_create_station_requires_an_identity(session):
    fake = FakeStationClient()
    out = station_ops.create_station(session, "ghost", "nobody",
                                     client=fake, now=NOW)
    assert out["status"] == "not_found"
    assert fake.created == []


# ── status: the timeout backstop ────────────────────────────────────────────
def test_station_status_timeout_backstop(session):
    iid = _ident("rmfyalk", "acc-t")
    sid = _station(iid, "rmfyalk", "acc-t", status="waiting_operator",
                   deadline=NOW + dt.timedelta(seconds=1500))

    # in flight before the deadline
    rows = station_ops.station_status(session, now=NOW)
    assert rows[0]["status"] == "waiting_operator" and rows[0]["live"]

    # one second past it: closed as timeout, finished_at set, evented
    late = NOW + dt.timedelta(seconds=1501)
    rows = station_ops.station_status(session, now=late)
    assert rows[0]["status"] == "timeout" and not rows[0]["live"]
    s = get_database().get_session()
    try:
        row = s.get(CrawlLoginStation, sid)
        assert station_ops._as_aware(row.finished_at) == late
    finally:
        s.close()
    assert any("timed out" in d for d in _events(iid))

    # idempotent: a later pass neither resurrects nor re-events
    rows = station_ops.station_status(session, now=late + dt.timedelta(minutes=5))
    assert rows[0]["status"] == "timeout"
    assert sum("timed out" in d for d in _events(iid)) == 1


def test_station_status_leaves_terminal_rows_alone(session):
    iid = _ident("rmfyalk", "acc-done")
    sid = _station(iid, "rmfyalk", "acc-done", status="completed",
                   deadline=NOW - dt.timedelta(hours=1),
                   finished_at=NOW - dt.timedelta(hours=1))
    rows = station_ops.station_status(session, now=NOW + dt.timedelta(hours=2))
    row = next(r for r in rows if r["id"] == sid)
    assert row["status"] == "completed"  # history is not rewritten


def test_station_status_masks_proxy_credentials(session):
    iid = _ident("rmfyalk", "acc-m")
    _station(iid, "rmfyalk", "acc-m", proxy_url="http://user:pass@1.2.3.4:8080")
    rows = station_ops.station_status(session, now=NOW)
    assert rows[0]["proxy_url_masked"] == "http://••••@1.2.3.4:8080"
    assert "user:pass" not in json.dumps(rows)


def test_active_stations_summary(session):
    iid = _ident("rmfyalk", "acc-s")
    _station(iid, "rmfyalk", "acc-s", deadline=NOW + dt.timedelta(seconds=60))
    _station(iid, "rmfyalk", "acc-s", status="completed",
             deadline=NOW - dt.timedelta(hours=1))
    assert station_ops.active_stations_summary(session, now=NOW) == \
        {"active": 1}
    # the summary applies the backstop too
    assert station_ops.active_stations_summary(
        session, now=NOW + dt.timedelta(hours=1)) == {"active": 0}


# ── reclaim ─────────────────────────────────────────────────────────────────
def test_reclaim_station_deletes_objects_and_closes_row(session):
    _source("rmfyalk")
    station_ops.ensure_identity_with_egress(session, "rmfyalk", "acc-r", now=NOW)
    fake = FakeStationClient()
    out = station_ops.create_station(session, "rmfyalk", "acc-r",
                                     client=fake, now=NOW)
    sid = out["station_id"]

    res = station_ops.reclaim_station(session, sid, client=fake, now=NOW)
    assert res["status"] == "reclaimed"
    # deleted carries the object names (Job + Service share the station name)
    assert sorted(kind for kind, _ in fake.deleted) == ["Job", "Service"]
    assert fake.created == []  # both cluster objects gone
    s = get_database().get_session()
    try:
        row = s.get(CrawlLoginStation, sid)
        assert row.status == "reclaimed"
        assert station_ops._as_aware(row.finished_at) == NOW
    finally:
        s.close()
    # the identity keeps its own machine (login_required) — only an event
    s = get_database().get_session()
    try:
        ident = s.get(CrawlIdentity, out["identity_id"])
        s.refresh(ident)
        assert ident.status == "login_required"
    finally:
        s.close()
    assert any("reclaimed by panel" in d for d in _events(out["identity_id"]))

    # second reclaim: terminal, idempotent, nothing left to delete
    res2 = station_ops.reclaim_station(session, sid, client=fake, now=NOW)
    assert res2["status"] == "already_finished"
    assert res2["deleted"] == []


def test_reclaim_terminal_station_keeps_status_but_cleans_objects(session):
    iid = _ident("rmfyalk", "acc-c")
    _source("rmfyalk")
    fake = FakeStationClient()
    created = station_ops.create_station(session, "rmfyalk", "acc-c",
                                         client=fake, now=NOW)
    sid = created["station_id"]
    # the runtime reported completion
    s = get_database().get_session()
    try:
        row = s.get(CrawlLoginStation, sid)
        row.status = "completed"
        row.finished_at = NOW + dt.timedelta(minutes=5)
        s.commit()
    finally:
        s.close()

    res = station_ops.reclaim_station(session, sid, client=fake, now=NOW)
    assert res["status"] == "already_finished"
    assert res["current_status"] == "completed"
    assert fake.created == []
    s = get_database().get_session()
    try:
        assert s.get(CrawlLoginStation, sid).status == "completed"
    finally:
        s.close()


def test_reclaim_unknown_station(session):
    fake = FakeStationClient()
    out = station_ops.reclaim_station(session, 999, client=fake, now=NOW)
    assert out["status"] == "not_found"
