"""到期告警：把"剩余 < 寿命 1/3"接到已有的告警通道上。

为什么需要它：3.2 的巡检脚本能算出风险，但没人会每天手动跑。我把 cheap 的凭据换成了
30 天到期的 bound token——如果刷新没在跑，到期那天派发会**静默**变成 401。
这个 Job 就是那条"到期之前先说一声"的线。

复用而不是重写：
  - 寿命判定 import 自 `check_worker_credential_surface.credential_lifetime`
  - 投递走 `visibility.notifiers.get_notifier()`（缺 token 只记日志、不抛）
"""
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "alert_stale_credentials.py"
SURFACE = Path(__file__).resolve().parents[1] / "scripts" / "check_worker_credential_surface.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load():
    # alert 脚本靠 `from check_worker_credential_surface import ...` 找邻居，
    # 所以先把邻居装进 sys.modules 并把 scripts/ 放进 sys.path。
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    _load("check_worker_credential_surface", SURFACE)
    return _load("alert_stale_credentials", SCRIPT)


def _jwt(claims):
    import base64, json

    def seg(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def _dir(tmp_path, **tokens):
    for name, tok in tokens.items():
        (tmp_path / f"{name}.token").write_text(tok, encoding="utf-8")
    (tmp_path / "cheap.ca").write_text("ca", encoding="utf-8")
    return tmp_path


def test_healthy_tokens_stay_silent(tmp_path, capsys):
    import time

    m = load()
    now = int(time.time())   # credential_lifetime 内部取真实时钟
    _dir(tmp_path, cheap=_jwt({"iat": now - 86400, "exp": now + 29 * 86400}))
    rc = m.main(["--kubeconfigs-dir", str(tmp_path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK 没有到期风险" in out
    assert "ALERT" not in out


def test_stale_token_alerts_and_names_the_fix(tmp_path, capsys):
    import time

    m = load()
    now = int(time.time())   # credential_lifetime 内部取真实时钟
    # 寿命 30d，只剩 3d（< 10d）
    _dir(tmp_path, cheap=_jwt({"iat": now - 27 * 86400, "exp": now + 3 * 86400}))
    rc = m.main(["--kubeconfigs-dir", str(tmp_path), "--dry-run"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "ALERT" in out
    assert "1/3" in out
    # 告警必须带可直接执行的处理办法，否则收到的人还得先查文档
    assert "rotate_worker_credential" in out


def test_expired_token_alerts(tmp_path, capsys):
    import time

    m = load()
    now = int(time.time())   # credential_lifetime 内部取真实时钟
    _dir(tmp_path, cheap=_jwt({"iat": now - 40 * 86400, "exp": now - 86400}))
    rc = m.main(["--kubeconfigs-dir", str(tmp_path), "--dry-run"])

    assert rc == 1
    assert "已过期" in capsys.readouterr().out


def test_legacy_token_is_not_an_expiry_risk(tmp_path, capsys):
    """legacy 没有 exp —— 它不会"快到期"，那是它另一个问题（永不过期）。"""
    m = load()
    _dir(tmp_path, cheap=_jwt({"kubernetes.io/serviceaccount/namespace": "scraw"}))
    rc = m.main(["--kubeconfigs-dir", str(tmp_path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "永不过期" in out


def test_missing_dir_is_rc2_not_silence(tmp_path, capsys):
    m = load()
    rc = m.main(["--kubeconfigs-dir", str(tmp_path / "nope"), "--dry-run"])
    assert rc == 2
    assert "不存在" in capsys.readouterr().err


def test_unreadable_token_file_does_not_look_healthy(tmp_path, capsys):
    """读不到凭据 ≠ 没有风险；要显示成 unknown 并留下 error。"""
    m = load()
    p = tmp_path / "cheap.token"
    p.write_text("garbage", encoding="utf-8")
    rc = m.main(["--kubeconfigs-dir", str(tmp_path), "--dry-run"])
    assert rc == 0                      # unknown 不算到期风险
    assert "寿命未知" in capsys.readouterr().out
