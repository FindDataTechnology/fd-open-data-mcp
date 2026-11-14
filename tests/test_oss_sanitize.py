"""OSS sanitization gate (task 6.4): catches planted violations by class.

The spec scenario: drop a file containing a credential pattern into the tree →
the check blocks it and names the location. Covers all three classes.
"""
from __future__ import annotations

from pathlib import Path

from scripts.oss_sanitize import scan


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_clean_tree_passes(tmp_path):
    _write(tmp_path, "app.py", "import os\nprint(os.environ.get('X'))\n")
    assert scan(tmp_path) == []


def test_credential_detected(tmp_path):
    _write(tmp_path, "config.py",
           'API_KEY = "sk-live-abcdefghijklmnop"\n')
    v = scan(tmp_path)
    assert any(x["kind"] == "credential" and "config.py" in x["file"]
               for x in v)


def test_private_key_detected(tmp_path):
    _write(tmp_path, "id.pem", "-----BEGIN RSA PRIVATE KEY-----\nfoo\n")
    v = scan(tmp_path)
    # a .pem path trips the topology rule; a key literal in a text file would
    # trip the credential rule — either way it must be caught
    assert any(x["kind"] in ("credential", "topology") for x in v)


def test_dsn_password_detected(tmp_path):
    _write(tmp_path, "db.py",
           'url = "postgresql://user:secretpass@host/db"\n')
    v = scan(tmp_path)
    assert any(x["kind"] == "credential" for x in v)


def test_internal_host_detected(tmp_path):
    _write(tmp_path, "deploy.yaml", "image: harbor.local/foo/bar:latest\n")
    v = scan(tmp_path)
    assert any(x["kind"] == "internal_host" for x in v)


def test_tailnet_ip_detected(tmp_path):
    _write(tmp_path, "notes.md", "connect to 100.64.0.3:30432\n")
    v = scan(tmp_path)
    assert any(x["kind"] == "internal_host" for x in v)


def test_topology_path_detected(tmp_path):
    _write(tmp_path, "secrets/token.txt", "whatever\n")
    v = scan(tmp_path)
    assert any(x["kind"] == "topology" and "secrets/" in x["file"] for x in v)


def test_env_var_name_not_flagged(tmp_path):
    """An env-var NAME (not a literal value) must NOT trip the credential rule."""
    _write(tmp_path, "app.py",
           'api_key = os.environ.get("POLYGON_API_KEY")\n'
           'password = os.getenv("DB_PASSWORD")\n')
    assert scan(tmp_path) == []


def test_violation_reports_location(tmp_path):
    _write(tmp_path, "leak.py", 'x = 1\nsecret = "abcdefghijklmnop"\n')
    v = scan(tmp_path)
    cred = [x for x in v if x["kind"] == "credential"]
    assert cred and cred[0]["file"] == "leak.py" and cred[0]["line"] == 2


def test_the_scanner_does_not_flag_its_own_rulebook(tmp_path):
    """扫描器自己的规则表必须含那些字符串，扫自己必然命中。

    2026-11-14 实测：`Build Check` 在 main 上连红多个提交，其中 3 条就是
    `scripts/oss_sanitize.py` 命中了它自己的规则（文档字符串里的 tailnet 网段、
    规则列表里的两个私有主机名）。这 3 条假红把 72 条真命中压在了下面。
    排除规则本不是放宽策略——规则本自己就是要公开的，里面装的是检测器不是秘密。
    """
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "scripts" / "oss_sanitize.py").read_text(encoding="utf-8")
    assert "lawcraw_business" in src          # 规则表里确实有这些串
    assert "100.64" in src

    _write(tmp_path, "scripts/oss_sanitize.py", src)
    assert scan(tmp_path) == []


def test_the_rulebook_does_not_hide_a_real_violation(tmp_path):
    """反面对照：排除的是「文件自己」，不是「文件所在目录」——
    同目录里另放一个真泄漏必须照样抓到。"""
    repo = Path(__file__).resolve().parent.parent
    src = (repo / "scripts" / "oss_sanitize.py").read_text(encoding="utf-8")
    _write(tmp_path, "scripts/oss_sanitize.py", src)
    _write(tmp_path, "scripts/leak.py", "HOST = 'guangzhou-xinru'\n")
    found = scan(tmp_path)
    assert [v["file"] for v in found] == ["scripts/leak.py"]
