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
