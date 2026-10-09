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
