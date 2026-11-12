"""凭据到期检查折进 visibility.scan 之后的行为（rotate-worker-cluster-credentials 3.2）。

为什么要有这组测试：这条检查原先住在一条**独立 CronJob** 里，而那条 CronJob 自己
失败时同样悄无声息——"到期告警"本身没有到期告警。折进 scan 的理由是 scan 已经
每 15 分钟跑一次、已经带推送通道和去重。把检查搬进来之后，必须钉住四件事：

1. **没挂载 ≠ 有风险**：目录不存在时静默跳过（`credentials_checked=False`），
   不能把每个未挂载的部署变成永久假红。
2. **legacy / 不可解的 token 不告警**：前者是选择，后者是不知道；只有"bound 快到期 /
   已过期"才是"今天不处理明天就断"。
3. **它是持续条件，不是一次性事件**：去重窗口 24h，不是失败告警用的 7 天。
4. **它和失败告警合并成一条消息**：不额外消耗 ServerChan 额度，也不能把
   CRAWL FAILURE 的标题挤掉。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
from unittest.mock import MagicMock

import pytest

from fd_open_data_mcp.visibility import scan, state

DAY = 86400


def _jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.c2ln"


def _bound(remaining_days: float, lifetime_days: float = 30.0) -> str:
    now = time.time()
    return _jwt({"iat": int(now - (lifetime_days - remaining_days) * DAY),
                 "exp": int(now + remaining_days * DAY)})


@pytest.fixture(autouse=True)
def _mounted_cluster_is_a_real_cluster(session):
    """挂了 `cheap.token`，`clusters` 表里就必须有一行且 `enabled=true`。

    本文件测的是"到期"，而 scan 现在同时判"挂载面 vs 表"：挂着一个表里没有的凭据
    就是**孤儿**，会另发一条告警，让这里的 `send.call_count == 0` 断言变成假红。
    这不是放宽判定，而是让用例的场景自洽——线上挂了凭据的集群一定在表里。
    """
    from fd_open_data_mcp.models import Cluster

    if session.query(Cluster).count() == 0:
        session.add(Cluster(name="cheap", api_server="https://cheap:6443",
                            namespace="scraw", enabled=True))
        session.commit()


@pytest.fixture
def creds_dir(tmp_path, monkeypatch):
    """Redirect the mount point to a tmp dir; returns a writer helper."""
    d = tmp_path / "kubeconfigs"
    d.mkdir()
    monkeypatch.setattr(scan, "_CREDENTIALS_DIR", str(d))

    def put(name: str, token: str) -> None:
        (d / f"{name}.token").write_text(token, encoding="utf-8")

    put.dir = d
    return put


@pytest.fixture
def sent(monkeypatch):
    """Capture sends; alert-at-least-once dedup (no Redis)."""
    box = MagicMock()
    monkeypatch.setattr(scan, "get_notifier", lambda: box)
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda *a, **k: None)
    return box


def _scan(session, now=None):
    return scan.scan_once(session, now=now or dt.datetime.now(dt.timezone.utc))


def test_missing_mount_skips_without_alert(session, monkeypatch, sent, tmp_path):
    monkeypatch.setattr(scan, "_CREDENTIALS_DIR", str(tmp_path / "nope"))
    summary = _scan(session)
    assert summary["credentials_checked"] is False
    assert summary["credential"] == []
    assert sent.send.call_count == 0


def test_healthy_bound_token_is_silent(session, creds_dir, sent):
    creds_dir("cheap", _bound(remaining_days=25.0))  # 剩 25/30 = 83%
    summary = _scan(session)
    assert summary["credentials_checked"] is True
    assert summary["credential"] == []
    assert sent.send.call_count == 0


def test_mounted_but_empty_is_not_green(session, creds_dir, sent):
    """挂载点在、但一个 `.token` 都没有 ⇒ 不算查过，而且是"缺凭据"。

    实测坏读数（2026-11-12 线上输出）：`credentials_checked: true` +
    `credential: []`，而健康时也是这两个值——两种情形完全同形。于是只要密钥键
    从 `cheap.token` 改成别的、或 Secret 被换空，到期告警会**静默变哑**，
    唯一的外部判据（那个 true）还在说"查过了"。判据必须落到"真评过 token"。

    2026-11-12 起还有第二道：挂载面比对会把"启用的集群缺凭据"单独报出来
    （surface 的 `missing`）。所以这条用例现在断言两件事——到期检查保持沉默但
    明说没查过，而**面检查会告警**。空挂载在任何意义上都不再是绿色的。
    """
    (creds_dir.dir / "cheap.ca").write_text("CA", encoding="utf-8")  # 只有证书没有 token
    summary = _scan(session)
    assert summary["credentials_checked"] is False, (
        "目录在但零个 token 不能算 checked——否则与健康同形")
    assert summary["credential"] == []          # 到期这一路：无话可说，且明说没查过
    # 面这一路：cheap 是启用的，却没有 token —— 必须报出来
    assert [(r["cluster"], r["kind"]) for r in summary["credential_surface"]] == [
        ("cheap", "missing")]
    assert sent.send.call_count == 1
    title, body = sent.send.call_args[0][:2]
    assert "CREDENTIAL SURFACE" in title and "401" in body


def test_stale_bound_token_alerts_with_its_own_title(session, creds_dir, sent):
    # 寿命 30d、只剩 2d → 远低于 1/3
    creds_dir("cheap", _bound(remaining_days=2.0))
    summary = _scan(session)
    assert summary["credentials_checked"] is True
    assert [c["cluster"] for c in summary["credential"]] == ["cheap"]
    assert summary["credential"][0]["expired"] is False
    sent.send.assert_called_once()
    title, body = sent.send.call_args[0][:2]
    assert "WORKER CREDENTIAL" in title
    assert "1/3" in body and "cheap" in body
    # 刷新命令要能直接抄走
    assert "--cluster cheap" in body


def test_expired_token_says_so(session, creds_dir, sent):
    creds_dir("cheap", _jwt({"iat": int(time.time()) - 40 * DAY,
                             "exp": int(time.time()) - 1 * DAY}))
    summary = _scan(session)
    assert summary["credential"][0]["expired"] is True
    title, body = sent.send.call_args[0][:2]
    assert "已过期" in body
    assert "401" in body


def test_legacy_token_is_not_a_fault(session, creds_dir, sent):
    # 无 exp：永不失效，也是永不兜底。是选择，不是故障。
    creds_dir("cheap", _jwt({"iat": int(time.time()) - 10 * DAY}))
    summary = _scan(session)
    assert summary["credentials_checked"] is True
    assert summary["credential"] == []
    assert sent.send.call_count == 0


def test_unparseable_token_is_not_guessed(session, creds_dir, sent):
    creds_dir("cheap", "not-a-jwt")
    summary = _scan(session)
    assert summary["credentials_checked"] is True
    assert summary["credential"] == []
    assert sent.send.call_count == 0


def test_already_alerted_is_suppressed(session, creds_dir, monkeypatch):
    box = MagicMock()
    monkeypatch.setattr(scan, "get_notifier", lambda: box)
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: True)
    monkeypatch.setattr(state, "mark_alerted", lambda *a, **k: None)
    creds_dir("cheap", _bound(remaining_days=2.0))
    summary = _scan(session)
    assert summary["credential"] == []
    assert box.send.call_count == 0


def test_dedup_uses_24h_not_the_7day_run_window(session, creds_dir, monkeypatch):
    """持续为真的条件必须按天重提；7 天静默对 10 天跑道太长。"""
    box = MagicMock()
    calls: list[tuple] = []
    monkeypatch.setattr(scan, "get_notifier", lambda: box)
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted",
                        lambda *a, **k: calls.append((a, k)))
    creds_dir("cheap", _bound(remaining_days=2.0))
    _scan(session)
    assert calls, "应当记录一次已告警"
    args, kwargs = calls[-1]
    assert args[0] == "credential:cheap"
    assert args[1] == "credential"
    assert kwargs.get("ttl_sec") == 24 * 3600


def test_credential_folds_into_the_failure_message(session, creds_dir, sent, monkeypatch):
    """一次 tick 只发一条；失败标题优先，凭据问题附带在同一正文里。"""
    from fd_open_data_mcp.models import CrawlPolicy, PolicyRun

    p = CrawlPolicy(name="p1", enabled=True, concept_ids=[1], entity_type="fund",
                    cron_expr="0 6 * * *", timezone="UTC",
                    date_policy={"mode": "since_last"}, frequency="daily",
                    mode="per_date")
    session.add(p)
    session.commit()
    session.refresh(p)
    session.add(PolicyRun(policy_id=p.id, status="failed",
                          started_at=dt.datetime.now(dt.timezone.utc),
                          finished_at=dt.datetime.now(dt.timezone.utc),
                          detail="boom"))
    session.commit()

    monkeypatch.setattr(scan.snapshot, "stale_runs", lambda *a, **k: [])
    monkeypatch.setattr(state, "get_scan_watermark", lambda: None)
    monkeypatch.setattr(state, "set_scan_watermark", lambda ts: None)

    creds_dir("cheap", _bound(remaining_days=2.0))
    summary = _scan(session)

    assert len(summary["failed"]) == 1
    assert len(summary["credential"]) == 1
    sent.send.assert_called_once()
    title, body = sent.send.call_args[0][:2]
    assert "CRAWL FAILURE" in title
    assert "WORKER CREDENTIAL" not in title
    assert "boom" in body and "1/3" in body


@pytest.mark.parametrize("clusters", [1, 2, 9, 12])
def test_alert_title_survives_the_32_char_budget(session, creds_dir, sent, clusters):
    """标题被截到 32 字，集群数必须还在里面。

    实测过的坏读数：`🔑 WORKER CREDENTIAL · 1 cluster(s)` 是 38 字，被截成
    `🔑 WORKER CREDENTIAL · 1 cluster(` ——**单位被吃掉，只剩一个开括号**。
    一条告警最该传达的就是"几个集群"，它不该是第一个被截掉的东西。
    """
    for i in range(clusters):
        creds_dir(f"c{i}", _bound(remaining_days=2.0))
    _scan(session)
    title, _ = sent.send.call_args[0][:2]
    assert len(title) <= 32, (len(title), title)
    assert str(clusters) in title, title
    assert not title.endswith("("), title
