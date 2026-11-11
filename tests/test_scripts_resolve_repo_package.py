"""脚本能否自己找到本仓的包（rotate-worker-cluster-credentials 3.2 折进 scan 之后）。

**为什么需要这组测试**：`scripts/` 不是包，`python3 scripts/x.py` 会把 `scripts/`
放进 `sys.path[0]`，于是裸 `import fd_open_data_mcp` 解析的是**环境里装的那个包**：

- CI runner（`refresh-worker-credentials.yml`）只 checkout、不 `pip install` ——
  那里根本没有这个包，脚本一运行就是 `ModuleNotFoundError`；
- 开发机上往往装的是**另一个检出**（editable install 指向别处），于是脚本拿旧代码
  做判定，而且不报错。

两种都只在"跑起来"的那一刻暴露，静态检查看不见。所以脚本显式把本仓根塞进
`sys.path`，这里用**子进程**验证它真的生效：子进程里 `scripts/` 仍然是 sys.path[0]，
和 CI 与人手执行的形态一致。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 不靠外部 PYTHONPATH：脚本必须自己解决
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, *args], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=120, **kw)


def test_surface_check_runs_without_an_installed_package(tmp_path):
    """没有 Pip 装的包时也要能跑：否则 CI 的 --report-only 步骤永远 ModuleNotFoundError。"""
    clusters = tmp_path / "clusters.json"
    clusters.write_text('[{"name": "cheap", "enabled": true}]', encoding="utf-8")
    kc = tmp_path / "kc"
    kc.mkdir()

    r = _run([str(SCRIPTS / "check_worker_credential_surface.py"),
              "--kubeconfigs-dir", str(kc),
              "--clusters-json", str(clusters),
              "--report-only"])
    assert r.returncode == 0, r.stderr
    assert "REPORT-ONLY" in r.stdout
    # 走到了判定逻辑，而不是在 import 处炸掉
    assert "clusters 表: 1 个" in r.stdout


def test_alert_script_imports_the_repo_package(tmp_path):
    """`credentials` 模块只存在于本仓的包里；能算出寿命即证明用的是本仓那份。"""
    import base64
    import json
    import time

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    now = int(time.time())
    (tmp_path / "cheap.token").write_text(
        f"{seg({'alg': 'RS256'})}.{seg({'iat': now - 5 * 86400, 'exp': now + 25 * 86400})}.sig",
        encoding="utf-8")

    r = _run([str(SCRIPTS / "alert_stale_credentials.py"),
              "--kubeconfigs-dir", str(tmp_path), "--dry-run"])
    assert r.returncode == 0, r.stderr
    assert "剩余" in r.stdout and "OK 没有到期风险" in r.stdout


def test_alert_script_names_the_problem_when_the_package_is_too_old(tmp_path):
    """镜像里的包比脚本旧时，必须点名，而不是抛一个没人读得懂的裸 traceback。

    这是本 change 自己引入的时序风险：脚本进 ConfigMap 比镜像进集群快，
    于是"到期告警"可能因为 ImportError 而哑掉——正好是它要防的那件事。

    构造方式刻意做成**真实形态**：把脚本复制到挂载点那样的目录（祖先里没有
    `fd_open_data_mcp/`，于是 bootstrap 不生效），再用 PYTHONPATH 造一个**旧版包**
    （有 visibility、没有 credentials）。这样测的是"包旧了"这条路，而不是
    "机器上碰巧装了哪个版本"。
    """
    # 1. 一个旧版包：有 visibility，没有 credentials
    fake = tmp_path / "fakepkg"
    (fake / "fd_open_data_mcp" / "visibility").mkdir(parents=True)
    (fake / "fd_open_data_mcp" / "__init__.py").write_text("__version__ = '0.5.61'\n", encoding="utf-8")
    (fake / "fd_open_data_mcp" / "visibility" / "__init__.py").write_text("", encoding="utf-8")

    # 2. 脚本挂在类似 /opt/credential-alert/ 的位置
    mounted = tmp_path / "credential-alert"
    mounted.mkdir()
    script = mounted / "alert_stale_credentials.py"
    script.write_text((SCRIPTS / "alert_stale_credentials.py").read_text(encoding="utf-8"),
                      encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake)   # 旧包排在 site-packages 之前
    r = subprocess.run([sys.executable, str(script), "--kubeconfigs-dir", str(tmp_path)],
                       cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "0.5.62" in r.stderr
    assert "ConfigMap" in r.stderr
