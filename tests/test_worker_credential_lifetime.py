"""凭据剩余寿命的判定逻辑（rotate-worker-cluster-credentials 3.2）。

要守的两件事：

1. **bound 的到期是静默的**——不能等到某天派发 401 才发现。剩余寿命必须能从
   token 本身读出来，且"刷新停了"要在到期之前就亮（剩余 < 寿命 1/3）。
2. **legacy 没有 exp 不等于健康**——它意味着"永不失效，也永不兜底"。输出必须
   明说这件事，而不是显示成"未知"或干脆留空，否则看板会把它读成没问题。

判据一律取 token 里的 iat/exp，不引入外部记账文件（那种账本会和真实凭据漂移）。
"""

import base64
import importlib.util
import json
import pathlib
import sys
import time

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "scripts" / "check_worker_credential_surface.py")


def load():
    spec = importlib.util.spec_from_file_location("cred_surface", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cred_surface"] = mod
    spec.loader.exec_module(mod)
    return mod


def _jwt(claims: dict, header: dict | None = None) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg(header or {'alg': 'RS256'})}.{seg(claims)}.c2ln"


def test_bound_token_reports_its_remaining_life():
    m = load()
    now = 1_000_000.0
    # 寿命 30d：签发在 1 天前，到期在 29 天后
    token = _jwt({"iat": int(now) - 86400, "exp": int(now) + 29 * 86400})

    life = m.credential_lifetime(token, now=now)

    assert life["kind"] == "bound"
    assert life["expires_at"] == int(now) + 29 * 86400
    assert life["lifetime_s"] == 30 * 86400
    assert life["remaining_s"] == 29 * 86400
    assert life["stale"] is False
    assert "剩余" in m.describe_lifetime(life)


def test_refresh_stopping_is_visible_before_expiry():
    """剩余 < 寿命的 1/3 就该报——这是"刷新没在跑"，不是"快到期了"。

    与 3.3 的刷新节奏（间隔 ≤ 寿命 1/3）是同一刻度的两端：正常刷新时永远不亮。
    """
    m = load()
    now = 1_000_000.0
    # 寿命 30d，只剩 5d（< 10d）
    token = _jwt({"iat": int(now) - 25 * 86400, "exp": int(now) + 5 * 86400})

    life = m.credential_lifetime(token, now=now)

    assert life["stale"] is True
    problems = m.lifetime_problems({"cheap": life})
    assert len(problems) == 1
    assert "1/3" in problems[0]
    assert "刷新" in problems[0]


def test_expired_bound_token_is_a_distinct_violation():
    """过期和"该刷新了"是两种处置：前者此刻就 401。"""
    m = load()
    now = 1_000_000.0
    token = _jwt({"iat": int(now) - 40 * 86400, "exp": int(now) - 86400})

    problems = m.lifetime_problems({"cheap": m.credential_lifetime(token, now=now)})

    assert len(problems) == 1
    assert "已过期" in problems[0]


def test_legacy_token_is_reported_as_never_expiring_not_as_healthy():
    m = load()
    token = _jwt({"kubernetes.io/serviceaccount/namespace": "scraw"})

    life = m.credential_lifetime(token, now=1_000_000.0)

    assert life["kind"] == "legacy"
    assert life["expires_at"] is None
    assert life["stale"] is None            # 不是"健康"，是"没有这个维度"
    assert "永不过期" in m.describe_lifetime(life)
    # legacy 是选择，不是故障：不产生违规
    assert m.lifetime_problems({"cheap": life}) == []


def test_unparseable_token_is_unknown_and_does_not_claim_a_lifetime():
    m = load()
    for bad in ("", "not-a-jwt", "a.b", "a.b.c"):
        life = m.credential_lifetime(bad, now=1_000_000.0)
        assert life["kind"] == "unknown", bad
        assert life["remaining_s"] is None
        assert "寿命未知" in m.describe_lifetime(life)


def test_cli_json_surface_changes_when_the_refresh_time_moves_back(tmp_path):
    """3.2 的验收方式：把刷新时间改早，健康输出必须跟着变。

    两个 fixture 只差 iat/exp 的偏移，其余完全一致——所以输出若不变，就说明
    判据没真的读 token（或者恒真）。
    """
    m = load()
    now = time.time()

    def write(token):
        (tmp_path / "cheap.token").write_text(token, encoding="utf-8")
        (tmp_path / "clusters.json").write_text(
            json.dumps([{"name": "cheap", "enabled": True}]), encoding="utf-8")

    write(_jwt({"iat": int(now) - 3600, "exp": int(now) + 29 * 86400}))
    fresh = _run(m, tmp_path)

    write(_jwt({"iat": int(now) - 790 * 3600, "exp": int(now) + 2 * 3600}))
    stale = _run(m, tmp_path)

    assert fresh["ok"] is True and fresh["problems"] == []
    assert stale["ok"] is False
    assert any("1/3" in p for p in stale["problems"])
    assert fresh["clusters"][0]["credential"]["remaining_s"] > \
        stale["clusters"][0]["credential"]["remaining_s"]


def _run(m, tmp_path, extra=()):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main(["--kubeconfigs-dir", str(tmp_path),
                     "--clusters-json", str(tmp_path / "clusters.json"),
                     "--json", *extra])
    out = json.loads(buf.getvalue())
    out["_rc"] = rc
    return out


def test_cli_still_fails_on_an_orphan_credential(tmp_path):
    """寿命之外的老判据不许退化：孤儿/禁用集群仍然算违规。"""
    m = load()
    (tmp_path / "ghost.token").write_text(
        _jwt({"iat": 1, "exp": 2}), encoding="utf-8")
    (tmp_path / "clusters.json").write_text(
        json.dumps([{"name": "cheap", "enabled": True}]), encoding="utf-8")

    out = _run(m, tmp_path)

    assert out["_rc"] == 1
    assert out["orphans"] == ["ghost"]
    # 同时报了"enabled 但没凭据"，两件事都要点名
    assert any("cheap" in p and "没有" in p for p in out["problems"])


def test_missing_mount_dir_is_still_rc2_not_empty(tmp_path):
    """挂载缺失 ≠ 没有凭据（这条是原脚本的核心断言，不能被新逻辑掩护）。"""
    m = load()
    (tmp_path / "clusters.json").write_text("[]", encoding="utf-8")

    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = m.main(["--kubeconfigs-dir", str(tmp_path / "nope"),
                     "--clusters-json", str(tmp_path / "clusters.json")])
    assert rc == 2
    assert "不存在" in err.getvalue()
