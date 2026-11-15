"""Login-station orchestration tests (login-station-console 3.1).

Covers station_ops on the per-test sqlite fixture: the egress allocator
(distinct egress per account, healthy-only, same-source exclusion, least-used,
idempotent rebind), the station row state machine (launching ->
waiting_operator -> timeout backstop / reclaim), the Job/Service manifests
against a FakeStationClient — the injectable boundary at which tests stop: no
k8s API, no pod, nothing really launched — and the 预拉起 pre-arm planner /
executor plus the standing auth-alert summary (change prearm-login-station).
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
def _source(name, site="tencent", auth_profile=None, schedule="0 6 * * *",
            enabled=True, **kw) -> str:
    """One crawl_sources row (site adopted if needed). Extra kwargs land on the
    row — the pre-arm tests use them for schedule_tz/kind/last_commit."""
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, site) is None:
            s.add(CrawlSite(id=site, enabled=True))
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(source=name, site=site, schedule=schedule,
                              enabled=enabled, auth_profile=auth_profile, **kw))
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


# ── operator-chosen egress (the "I can't pick my proxy" defect) ──────────────
def test_selectable_egresses_lists_healthy_only_with_masked_urls(session):
    _proxy(scheme="direct", ip="direct", port=None)               # not an egress
    _proxy(ip="10.9.9.9", retired_at=NOW)                         # retired
    p_ok = _proxy(ip="10.0.0.5", port=3128, auth="u:pw", label="gost-xinru")
    out = station_ops.selectable_egresses(session)
    assert [e["egress_ref"] for e in out] == [f"proxy:{p_ok}"]
    assert out[0]["label"] == "gost-xinru"
    assert "u:pw" not in out[0]["url_masked"]      # credentials never surface
    assert "••••" in out[0]["url_masked"]


def test_explicit_egress_ref_overrides_allocation(session):
    p1 = _proxy(ip="10.0.0.1")
    p2 = _proxy(ip="10.0.0.2", label="gost-cheap")
    out = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-a", now=NOW, egress_ref=f"proxy:{p2}")
    assert out["status"] == "queued"
    assert out["egress_ref"] == f"proxy:{p2}"          # the CHOSEN one, not p1
    assert out["proxy_url"] == "http://u:p@10.0.0.2:8080"
    # the manual pick is evented distinctly from the auto-assign
    assert any("manually set" in d for d in _events(out["identity_id"]))
    # ...and a re-ensure without the choice keeps the manual binding
    again = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-a", now=NOW)
    assert again["egress_ref"] == f"proxy:{p2}"


def test_explicit_egress_ref_replaces_existing_binding(session):
    p1 = _proxy(ip="10.0.0.1")
    p2 = _proxy(ip="10.0.0.2")
    iid = _ident("rmfyalk", "acc-a", "login_required", egress_ref=f"proxy:{p1}")
    out = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-a", now=NOW, egress_ref=f"proxy:{p2}")
    assert out["egress_ref"] == f"proxy:{p2}"
    s = get_database().get_session()
    try:
        assert s.get(CrawlIdentity, iid).egress_ref == f"proxy:{p2}"
    finally:
        s.close()


def test_explicit_egress_ref_rejections(session):
    p = _proxy(ip="10.0.0.1")
    _ident("rmfyalk", "holder", "active", egress_ref=f"proxy:{p}")
    pd = _proxy(scheme="direct", ip="direct", port=None)
    pr = _proxy(ip="10.9.9.9", retired_at=NOW)

    # taken by a sibling account of the same source -> conflict, nothing written
    out = station_ops.ensure_identity_with_egress(
        session, "rmfyalk", "acc-b", now=NOW, egress_ref=f"proxy:{p}")
    assert out["status"] == "conflict" and "holder" in out["reason"]
    # unknown, malformed, retired, direct -> invalid
    for ref in ("proxy:999999", "nonsense", f"proxy:{pr}", f"proxy:{pd}"):
        out = station_ops.ensure_identity_with_egress(
            session, "rmfyalk", "acc-b", now=NOW, egress_ref=ref)
        assert out["status"] == "invalid", ref
    # in every rejection case the identity still exists unbound
    s = get_database().get_session()
    try:
        row = (s.query(CrawlIdentity)
               .filter_by(source="rmfyalk", account_alias="acc-b").first())
        assert row is not None and row.egress_ref is None
    finally:
        s.close()


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
    assert js["activeDeadlineSeconds"] == \
        station_ops.station_deadline_seconds()           # the hard deadline (60 min default)
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
    assert env["FD_STATION_DEADLINE_SECONDS"]["value"] == \
        str(station_ops.station_deadline_seconds())
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

    # the row: launching -> waiting_operator, deadline = now + the window, egress recorded
    s = get_database().get_session()
    try:
        row = s.get(CrawlLoginStation, sid)
        assert row.status == "waiting_operator"
        assert station_ops._as_aware(row.deadline_at) == \
            NOW + dt.timedelta(seconds=station_ops.station_deadline_seconds())
        # 库里只存脱敏 egress（2026-11-14）：`proxy_url` 是 http://user:pass@host:port，
        # 带真口令。显示只需要「走哪个出口」，真口令只走 Job 的 env（上面已单独断言）。
        assert row.proxy_url == "http://••••@10.0.0.7:3128"
        assert "user:pw@" not in row.proxy_url
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
                   deadline=NOW + dt.timedelta(
                       seconds=station_ops.station_deadline_seconds()))

    # in flight before the deadline
    rows = station_ops.station_status(session, now=NOW)
    assert rows[0]["status"] == "waiting_operator" and rows[0]["live"]

    # one second past it: closed as timeout, finished_at set, evented
    late = NOW + dt.timedelta(seconds=station_ops.station_deadline_seconds() + 1)
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


# ── station window: FD_STATION_DEADLINE_SECONDS (25 min was too short) ──────
def test_station_deadline_seconds_default_and_override(monkeypatch):
    monkeypatch.delenv("FD_STATION_DEADLINE_SECONDS", raising=False)
    assert station_ops.STATION_DEADLINE_SECONDS == 3600      # 60 min default
    assert station_ops.station_deadline_seconds() == 3600

    monkeypatch.setenv("FD_STATION_DEADLINE_SECONDS", "2700")
    assert station_ops.station_deadline_seconds() == 2700

    # garbage/blank/zero degrade to the default — a typo must never disarm
    # the window (the operator would silently lose their login time)
    for bad in ("nonsense", "", "  ", "0", "-5", "12.5"):
        monkeypatch.setenv("FD_STATION_DEADLINE_SECONDS", bad)
        assert station_ops.station_deadline_seconds() == 3600, bad


# ── 预拉起 pre-arm: the planner ─────────────────────────────────────────────
# 2026-09-26 is a Saturday; 19:00 UTC = 2026-09-27 03:00 Beijing, so a
# '10 3 * * *' Asia/Shanghai schedule fires 10 minutes later at 19:10 UTC.
PREARM_NOW = dt.datetime(2026, 9, 26, 19, 0, 0, tzinfo=dt.timezone.utc)


def test_prearm_plan_skips_when_identity_is_fresh(session):
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    _ident("rmfyalk", "acct001", "active",
           last_probe_at=PREARM_NOW - dt.timedelta(hours=1))

    assert station_ops.prearm_plan(session, now=PREARM_NOW) == []


def test_prearm_plan_skips_when_identity_is_leased(session):
    """An identity in use by a running crawl is never a pre-arm target:
    ensure_identity_with_egress resets it to login_required and clears the
    lease, which would yank the session from under the live run (2026-10-09)."""
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    _ident("rmfyalk", "acct001", "active",
           last_probe_at=PREARM_NOW - dt.timedelta(hours=7),  # stale by probe
           lease_token="t" * 24, lease_owner="runner-x")

    assert station_ops.prearm_plan(session, now=PREARM_NOW) == []


def test_prearm_plan_flags_stale_identity_before_the_fire(session):
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    iid = _ident("rmfyalk", "acct001", "active",
                 last_probe_at=PREARM_NOW - dt.timedelta(hours=7))

    plan = station_ops.prearm_plan(session, now=PREARM_NOW)

    assert len(plan) == 1
    assert plan[0]["source"] == "rmfyalk"
    assert plan[0]["account_alias"] == "acct001"
    assert plan[0]["minutes"] == 10
    assert plan[0]["next_fire"].startswith("2026-09-26T19:10")
    assert plan[0]["reason"] == "stale or missing identity"


def test_prearm_plan_skips_profiles_without_any_identity(session):
    # no identity rows at all: registration is a deliberate human act
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    assert station_ops.prearm_plan(session, now=PREARM_NOW) == []


def test_prearm_plan_skips_when_a_station_is_already_open(session):
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    iid = _ident("rmfyalk", "acct001", "login_required")
    _station(iid, "rmfyalk", "acct001", status="waiting_operator",
             deadline=PREARM_NOW + dt.timedelta(minutes=30))

    assert station_ops.prearm_plan(session, now=PREARM_NOW) == []
    # ...but a terminal station does not block a fresh pre-arm
    s = get_database().get_session()
    try:
        s.get(CrawlLoginStation, 1).status = "completed"
        s.commit()
    finally:
        s.close()
    assert len(station_ops.prearm_plan(session, now=PREARM_NOW)) == 1


def test_prearm_plan_ignores_a_station_whose_deadline_passed(session):
    """2026-11-14 实历的回归：僵尸站不许永久挡住预拉起。

    station #72（rmfyalk/acct001）11-10T18:28 建、19:28 过期、`finished_at`
    一直为空（Job 被 deadline 杀掉，没来得及上报终态）。超时兜底是「读时生效」，
    而这个 5 分钟的后台循环从不读站表 ⇒ 那一行 `waiting_operator` 让预拉起从
    11-10 起每一轮都判「操作者正看着」，再也不开新站；panel 日志每 5 分钟一句
    `prearm: no station needed this pass`，而源从 11-05 起零产出。
    判据要按「有效窗口」表达：过了 deadline 的站等于已关。
    """
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    iid = _ident("rmfyalk", "acct001", "login_required")
    _station(iid, "rmfyalk", "acct001", status="waiting_operator",
             deadline=PREARM_NOW - dt.timedelta(minutes=30))

    assert len(station_ops.prearm_plan(session, now=PREARM_NOW)) == 1


def test_prearm_tick_closes_expired_station_before_planning(session, monkeypatch):
    """兜底那一半：过期站在 tick 里要真的被收成 timeout（留痕），不只是被计划忽略。"""
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    iid = _ident("rmfyalk", "acct001", "login_required")
    _station(iid, "rmfyalk", "acct001", status="waiting_operator",
             deadline=PREARM_NOW - dt.timedelta(minutes=30))
    monkeypatch.setattr(station_ops, "ensure_identity_with_egress",
                        lambda *a, **k: {"status": "queued"})
    monkeypatch.setattr(station_ops, "create_station",
                        lambda *a, **k: {"status": "launched", "station_id": 2})

    station_ops.prearm_tick(session, now=PREARM_NOW, client=object())

    s = get_database().get_session()
    try:
        assert s.get(CrawlLoginStation, 1).status == "timeout"
        assert s.get(CrawlLoginStation, 1).finished_at is not None
    finally:
        s.close()


# ── finished-station Service reaper ─────────────────────────────────────────
def _station_objects(client, station_id, source="rmfyalk", alias="acct001",
                     identity_id=1):
    """Put a station's real Job+Service manifests into the fake cluster."""
    job, svc = station_ops.build_station_manifests(
        station_id=station_id, source=source, account_alias=alias,
        identity_id=identity_id, proxy_url=None)
    client.create(job)
    client.create(svc)
    return job, svc


def test_reap_removes_a_finished_stations_service(session):
    """站 Service 没有 TTL：实测 fd-mcp 54 个 Service 里 53 个是站点残骸
    （2026-10-08 起每晚一个），而 Jobs 全被 ttlSecondsAfterFinished 清干净了。
    清扫器的判据是「终态行 + 已过 Job TTL 宽限」，Job 不碰（它自己有 TTL）。"""
    station_ops._REAPED_STATION_IDS.clear()
    iid = _ident("rmfyalk", "acct001", "login_required")
    sid = _station(iid, "rmfyalk", "acct001", status="completed",
                   finished_at=NOW)
    client = FakeStationClient()
    _station_objects(client, sid)

    deleted = station_ops.reap_station_services(
        session, client=client, now=NOW + dt.timedelta(hours=2))

    assert deleted == 1
    assert [k for k, _ in client.deleted] == ["Service"]
    assert client.manifests("Job")          # the Job is left to its own TTL


def test_reap_spares_live_and_recent_stations(session):
    station_ops._REAPED_STATION_IDS.clear()
    iid = _ident("rmfyalk", "acct001", "login_required")
    live = _station(iid, "rmfyalk", "acct001", status="waiting_operator",
                    deadline=NOW + dt.timedelta(minutes=30))
    fresh = _station(iid, "rmfyalk", "acct002", status="completed",
                     finished_at=NOW)
    client = FakeStationClient()
    _station_objects(client, live)
    _station_objects(client, fresh, alias="acct002")

    # 10 min after the finish: inside the Job's own 1h TTL, pair expires together
    assert station_ops.reap_station_services(
        session, client=client, now=NOW + dt.timedelta(minutes=10)) == 0
    assert client.deleted == []
    assert len(client.manifests("Service")) == 2


def test_reap_is_idempotent_per_process(session):
    station_ops._REAPED_STATION_IDS.clear()
    iid = _ident("rmfyalk", "acct001", "login_required")
    sid = _station(iid, "rmfyalk", "acct001", status="timeout",
                   finished_at=NOW)
    client = FakeStationClient()
    _station_objects(client, sid)
    later = NOW + dt.timedelta(hours=2)

    assert station_ops.reap_station_services(session, client=client,
                                             now=later) == 1
    # second tick: the station is cached as visited, no list call, no delete
    assert station_ops.reap_station_services(session, client=client,
                                             now=later) == 0
    assert [k for k, _ in client.deleted] == ["Service"]


def test_reap_retries_after_a_failed_delete_and_never_raises(session):
    station_ops._REAPED_STATION_IDS.clear()

    class BrokenOnce(FakeStationClient):
        def __init__(self):
            super().__init__()
            self.failed = False

        def delete(self, kind, name):
            if not self.failed:
                self.failed = True
                raise RuntimeError("forbidden")
            return super().delete(kind, name)

    iid = _ident("rmfyalk", "acct001", "login_required")
    sid = _station(iid, "rmfyalk", "acct001", status="failed",
                   finished_at=NOW)
    client = BrokenOnce()
    _station_objects(client, sid)
    later = NOW + dt.timedelta(hours=2)

    assert station_ops.reap_station_services(session, client=client,
                                             now=later) == 0
    assert client.manifests("Service")      # still there, retried below
    assert sid not in station_ops._REAPED_STATION_IDS
    assert station_ops.reap_station_services(session, client=client,
                                             now=later) == 1
    assert client.manifests("Service") == []


def test_reap_refuses_a_service_it_did_not_create(session):
    """名字必须是我们自己的前缀；只按标签认会误删别人的对象。"""
    station_ops._REAPED_STATION_IDS.clear()
    iid = _ident("rmfyalk", "acct001", "login_required")
    sid = _station(iid, "rmfyalk", "acct001", status="completed",
                   finished_at=NOW)
    client = FakeStationClient()
    client.create({"apiVersion": "v1", "kind": "Service",
                   "metadata": {"name": "somebody-elses-svc",
                                "labels": {"station-id": str(sid)}}})

    assert station_ops.reap_station_services(
        session, client=client, now=NOW + dt.timedelta(hours=2)) == 0
    assert client.deleted == []


def test_prearm_tick_reaps_leftover_station_services(session, monkeypatch):
    """清扫挂在 tick 里：一次真实运行就把历史残骸收掉，且不干扰计划。"""
    station_ops._REAPED_STATION_IDS.clear()
    iid = _ident("rmfyalk", "acct001", "login_required")
    old = _station(iid, "rmfyalk", "acct001", status="completed",
                   finished_at=NOW - dt.timedelta(days=5))
    client = FakeStationClient()
    _, svc = _station_objects(client, old)
    svc_name = svc["metadata"]["name"]
    monkeypatch.setattr(station_ops, "ensure_identity_with_egress",
                        lambda *a, **k: {"status": "queued"})
    monkeypatch.setattr(station_ops, "create_station",
                        lambda *a, **k: {"status": "launched", "station_id": 2})

    station_ops.prearm_tick(session, now=NOW, client=client)

    assert ("Service", svc_name) in client.deleted


# ── the transport: deleting a station's Job must take its pod along ──────────
def test_station_delete_cascades_so_the_pod_does_not_survive(monkeypatch):
    """裸 DELETE 的默认传播是 Orphan ⇒ Job 走了、pod 变成裸 pod 永远留着。

    2026-11-15 产线冒烟实测：三次 reclaim 留下三个 ownerReferences 为空的 pod，
    还在 Running（512Mi 请求 / 2Gi 上限、各钉着一个镜像）。没有任何东西能收它们：
    Job 没了所以没有 TTL，reaper 只管 Service，行已是终态，而 pod-gc 按设计
    不碰裸 pod。判据只能落在**请求本身**——集群侧「看起来删干净了」正是那个形态。
    """
    client = station_ops.StationK8sClient(namespace="fd-mcp")
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(client, "_in_cluster", lambda: True)
    monkeypatch.setattr(client, "_api",
                        lambda method, path, body=None: calls.append(
                            (method, path, body)) or {})

    assert client.delete("Job", "login-rmfyalk-acct001-abc123") is True

    method, path, body = calls[-1]
    assert method == "DELETE"
    assert path.endswith("/apis/batch/v1/namespaces/fd-mcp/jobs/"
                         "login-rmfyalk-acct001-abc123")
    assert body is not None, (
        "DELETE 没带 DeleteOptions —— API 默认按 Orphan 处理，Job 的 pod 会被"
        "摘掉 ownerReference 继续跑，且再没有东西能收它")
    assert body.get("propagationPolicy") == "Background", (
        f"传播策略是 {body.get('propagationPolicy')!r}，必须 Background："
        "Foreground 会让 5 分钟的预拉起循环等 pod 真正消失")


def test_station_delete_cascades_on_the_kubectl_transport(monkeypatch):
    """本地/dev 走 kubectl 那半也要带级联，否则两个传输的行为不一致。"""
    client = station_ops.StationK8sClient(namespace="fd-mcp")
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(client, "_in_cluster", lambda: False)
    monkeypatch.setattr(client, "_kubectl",
                        lambda *args, **kw: seen.append(args) or "")

    client.delete("Job", "login-x-y-abc123")

    args = seen[-1]
    assert "delete" in args and "login-x-y-abc123" in args
    assert any(a.startswith("--cascade") for a in args), (
        f"kubectl delete 没带 --cascade（收到 {args}）")


def test_prearm_plan_skips_fires_beyond_the_window(session):
    # 19:00 UTC -> next fire 19:10 (10 min, inside 45); at 18:00 the same fire
    # is 70 min out, beyond the window
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    _ident("rmfyalk", "acct001", "active",
           last_probe_at=PREARM_NOW - dt.timedelta(hours=7))

    early = PREARM_NOW - dt.timedelta(hours=1)
    assert station_ops.prearm_plan(session, now=early) == []
    assert len(station_ops.prearm_plan(session, now=PREARM_NOW)) == 1


def test_prearm_plan_picks_the_most_recently_logged_in_alias(session):
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    _ident("rmfyalk", "old", "active",
           last_login_at=PREARM_NOW - dt.timedelta(days=30),
           last_probe_at=PREARM_NOW - dt.timedelta(days=1))
    _ident("rmfyalk", "recent", "active",
           last_login_at=PREARM_NOW - dt.timedelta(days=2),
           last_probe_at=PREARM_NOW - dt.timedelta(days=1))

    plan = station_ops.prearm_plan(session, now=PREARM_NOW)
    assert [p["account_alias"] for p in plan] == ["recent"]


def test_prearm_plan_ignores_unrunnable_and_unauthenticated_sources(session):
    # no auth_profile: anonymous, nothing to log in
    _source("anon-src", schedule="10 3 * * *", schedule_tz="Asia/Shanghai")
    # auth_profile but no mirror/runner declaration: the dispatcher cannot run it
    _source("unlit-src", auth_profile="unlit", schedule="10 3 * * *",
            schedule_tz="Asia/Shanghai", kind="platform", last_commit=None)
    # disabled
    _source("off-src", auth_profile="off", schedule="10 3 * * *",
            schedule_tz="Asia/Shanghai", kind="platform", last_commit="abc",
            enabled=False)
    # unlit (schedule NULL)
    _source("dark-src", auth_profile="dark", schedule=None,
            kind="platform", last_commit="abc")
    for profile in ("anon-src", "unlit", "off", "dark"):
        _ident(profile, "acct", "login_required")

    assert station_ops.prearm_plan(session, now=PREARM_NOW) == []


def test_prearm_plan_defaults_to_utc_when_schedule_tz_is_null(session):
    # schedule '0 19 * * *' with no tz = 19:00 UTC; at 18:30 the fire is 30 min
    # out (inside the 45-min window)
    _source("utc-src", auth_profile="utc-prof", schedule="0 19 * * *",
            kind="platform", last_commit="abc")
    _ident("utc-prof", "acct", "login_required")

    now = dt.datetime(2026, 9, 26, 18, 30, tzinfo=dt.timezone.utc)
    plan = station_ops.prearm_plan(session, now=now)
    assert [p["source"] for p in plan] == ["utc-prof"]
    assert plan[0]["minutes"] == 30


def test_prearm_plan_opens_one_station_per_profile(session):
    """Two sources sharing one auth_profile are one login unit: a single
    station serves both, so the plan must not emit (and the tick must not
    launch) two stations for the same account."""
    _source("case-crawl", auth_profile="shared", schedule="10 3 * * *",
            schedule_tz="Asia/Shanghai", kind="federated",
            runner_command=["node", "bin/x.mjs"])
    _source("bulk-crawl", auth_profile="shared", schedule="10 3 * * *",
            schedule_tz="Asia/Shanghai", kind="federated",
            runner_command=["node", "bin/y.mjs"])
    _ident("shared", "acct001", "login_required")
    fake = FakeStationClient()

    plan = station_ops.prearm_plan(session, now=PREARM_NOW)
    assert [p["source"] for p in plan] == ["shared"]
    assert len(station_ops.prearm_tick(session, now=PREARM_NOW,
                                       client=fake)) == 1
    assert len(fake.manifests("Job")) == 1


def test_prearm_plan_ignores_a_bad_cron_instead_of_crashing(session):
    _source("bad-cron", auth_profile="bad", schedule="not a cron",
            kind="platform", last_commit="abc")
    _ident("bad", "acct", "login_required")
    assert station_ops.prearm_plan(session, now=PREARM_NOW) == []


# ── 预拉起 pre-arm: the executor ────────────────────────────────────────────
def test_prearm_tick_launches_station_for_stale_identity(session, monkeypatch):
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    _proxy(ip="10.0.0.1")
    iid = _ident("rmfyalk", "acct001", "active",
                 last_probe_at=PREARM_NOW - dt.timedelta(hours=7))
    fake = FakeStationClient()

    out = station_ops.prearm_tick(session, now=PREARM_NOW, client=fake)

    assert len(out) == 1
    assert out[0]["status"] == "launched"
    assert out[0]["source"] == "rmfyalk"
    assert out[0]["account_alias"] == "acct001"
    assert out[0]["station_id"] == 1
    assert len(fake.manifests("Job")) == 1 and len(fake.manifests("Service")) == 1
    s = get_database().get_session()
    try:
        st = s.get(CrawlLoginStation, 1)
        assert st.status == "waiting_operator"          # FakeStationClient succeeds
        assert st.account_alias == "acct001"
        assert st.deadline_at is not None
        # ensure_identity_with_egress reset the identity to login_required
        assert s.get(CrawlIdentity, iid).status == "login_required"
    finally:
        s.close()
    # the pre-arm launch is on the audit trail
    assert any("login station #1 launched" in d for d in _events(iid))


def test_prearm_tick_does_nothing_for_a_fresh_identity(session, monkeypatch):
    _source("rmfyalk-case-crawl", auth_profile="rmfyalk",
            schedule="10 3 * * *", schedule_tz="Asia/Shanghai",
            kind="federated", runner_command=["node", "bin/rmfyalk-crawl.mjs"])
    _ident("rmfyalk", "acct001", "active",
           last_probe_at=PREARM_NOW - dt.timedelta(hours=1))
    fake = FakeStationClient()

    assert station_ops.prearm_tick(session, now=PREARM_NOW, client=fake) == []
    assert fake.created == []
    s = get_database().get_session()
    try:
        assert s.query(CrawlLoginStation).count() == 0
    finally:
        s.close()


def test_prearm_tick_continues_past_one_failing_source(session, monkeypatch):
    """One source's launch failure must not stop the others: the loop is a
    convenience and a per-source error is logged, never fatal."""
    _source("src-a", auth_profile="prof-a", schedule="10 3 * * *",
            schedule_tz="Asia/Shanghai", kind="platform", last_commit="abc")
    _source("src-b", auth_profile="prof-b", schedule="10 3 * * *",
            schedule_tz="Asia/Shanghai", kind="platform", last_commit="abc")
    _ident("prof-a", "acc-a", "login_required")
    _ident("prof-b", "acc-b", "login_required")
    fake = FakeStationClient()
    real_create = station_ops.create_station

    def flaky_create(s, source, alias, client=None, now=None):
        if source == "prof-a":
            raise RuntimeError("kubeconfig exploded")
        return real_create(s, source, alias, client=client, now=now)

    monkeypatch.setattr(station_ops, "create_station", flaky_create)
    out = station_ops.prearm_tick(session, now=PREARM_NOW, client=fake)

    assert [x["source"] for x in out] == ["prof-b"]
    assert len(fake.manifests("Job")) == 1


# ── the standing alert summary (piece 3) ────────────────────────────────────
def test_auth_alert_summary_counts_login_required_and_stale(session):
    _ident("rmfyalk", "needs-login", "login_required")
    _ident("rmfyalk", "stale", "active",
           last_probe_at=NOW - dt.timedelta(hours=7))
    _ident("rmfyalk", "fresh", "active",
           last_probe_at=NOW - dt.timedelta(hours=1))

    assert station_ops.auth_alert_summary(session, now=NOW) == \
        {"login_required": 1, "stale": 1, "total": 3}


def test_auth_alert_summary_counts_never_probed_active_as_stale(session):
    # an active identity that was never probed is the same risk as an old probe
    _ident("rmfyalk", "never-probed", "active")
    _ident("rmfyalk", "banned", "banned")

    assert station_ops.auth_alert_summary(session, now=NOW) == \
        {"login_required": 0, "stale": 1, "total": 2}
    assert station_ops.auth_alert_summary(session, now=NOW)["stale"] == 1


def test_auth_alert_summary_respects_fresh_hours_env(session, monkeypatch):
    _ident("rmfyalk", "probed-3h-ago", "active",
           last_probe_at=NOW - dt.timedelta(hours=3))
    assert station_ops.auth_alert_summary(session, now=NOW)["stale"] == 0
    monkeypatch.setenv("FD_PREARM_FRESH_HOURS", "2")
    assert station_ops.auth_alert_summary(session, now=NOW)["stale"] == 1


def test_prearm_env_overrides(monkeypatch):
    monkeypatch.delenv("FD_PREARM_MINUTES", raising=False)
    monkeypatch.delenv("FD_PREARM_FRESH_HOURS", raising=False)
    assert station_ops.prearm_minutes() == 45
    assert station_ops.prearm_fresh_hours() == 6
    monkeypatch.setenv("FD_PREARM_MINUTES", "120")
    monkeypatch.setenv("FD_PREARM_FRESH_HOURS", "12")
    assert station_ops.prearm_minutes() == 120
    assert station_ops.prearm_fresh_hours() == 12
    monkeypatch.setenv("FD_PREARM_MINUTES", "junk")
    monkeypatch.setenv("FD_PREARM_FRESH_HOURS", "-1")
    assert station_ops.prearm_minutes() == 45
    assert station_ops.prearm_fresh_hours() == 6
