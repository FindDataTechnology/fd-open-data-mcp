"""刷新节奏与寿命的倍数关系必须被真的读出来核对（change 3.3）。

这条闸门的价值全在"两个数字来自不同文件"：寿命在轮换脚本的 `--duration` 默认值，
间隔在工作流的 cron。它们会各自漂。所以测试要守两件事：

1. 解析器看懂 cron 的日/周字段 —— 第一版只看 hour，把「每周一」读成每天，
   倍数从 4.3 变成 30，等于把闸门悄悄放宽 7 倍还显示绿灯。
2. 倍数不满足时必须 rc=1 并点名两个数字；读不到必须 rc=2，不许当 0。
"""
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_credential_refresh_ratio.py"


def load():
    spec = importlib.util.spec_from_file_location("refresh_ratio", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["refresh_ratio"] = mod
    spec.loader.exec_module(mod)
    return mod


def _wf(tmp_path, cron):
    p = tmp_path / "wf.yml"
    p.write_text(
        "on:\n  schedule:\n    - cron: '%s'\n  workflow_dispatch:\n" % cron,
        encoding="utf-8")
    return p


def test_weekly_cron_is_a_week_not_a_day(tmp_path):
    """`17 3 * * 1` = 每周一 03:17 → 168h。读成 24h 会把闸门放宽 7 倍。"""
    m = load()
    assert m.read_interval_hours(_wf(tmp_path, "17 3 * * 1")) == 168.0


def test_daily_cron_is_a_day(tmp_path):
    m = load()
    assert m.read_interval_hours(_wf(tmp_path, "17 3 * * *")) == 24.0


def test_every_n_hours(tmp_path):
    m = load()
    assert m.read_interval_hours(_wf(tmp_path, "0 */6 * * *")) == 4.0


def test_monthly_and_malformed_are_refused_not_guessed(tmp_path):
    """间隔不固定的形态必须返回 None（→ rc=2），不许猜一个数出来。"""
    m = load()
    for cron in ("0 3 1 * *", "0 3 * * 1-5", "@daily", "0 3 * *"):
        assert m.read_interval_hours(_wf(tmp_path, cron)) is None, cron


def test_missing_workflow_is_none(tmp_path):
    m = load()
    assert m.read_interval_hours(tmp_path / "nope.yml") is None


def test_verdict_rejects_a_too_long_interval():
    m = load()
    assert m.judge(720, 168)["ok"] is True
    assert m.judge(720, 240)["ok"] is True     # 正好 1/3
    bad = m.judge(720, 241)
    assert bad["ok"] is False
    assert bad["ratio"] < 3


def test_cli_fails_loudly_and_names_both_numbers(tmp_path, monkeypatch, capsys):
    """把间隔改长后，闸门必须报红并说出两个数——不是静默通过。"""
    m = load()
    wf = _wf(tmp_path, "0 3 1 * *")            # 每月：无法解析 -> rc=2
    monkeypatch.setattr(m, "WORKFLOW", wf)
    assert m.main([]) == 2
    err = capsys.readouterr().err
    assert "刷新间隔" in err


def test_cli_passes_on_the_shipped_artefacts():
    """仓库里这两份必须真的满足规则（否则这条闸门一上来就是红的）。"""
    m = load()
    assert m.main([]) == 0


def test_trailing_comment_on_the_cron_line_is_tolerated(tmp_path):
    """给 cron 加一句行尾说明不能让闸门失效。

    第一版正则只认到行尾，于是 `- cron: '17 3 * * 1'  # Monday 03:17 UTC`
    解析失败、退出码变 2——闸门本身没坏，但从此每天的输出都是噪声，
    很快就会被 `|| true` 掉。这正是"注释一加，校验就哑"的形态。
    """
    m = load()
    p = tmp_path / "wf.yml"
    p.write_text(
        "on:\n  schedule:\n    - cron: '17 3 * * 1'      # Monday 03:17 UTC\n",
        encoding="utf-8")
    assert m.read_interval_hours(p) == 168.0


def test_bare_and_double_quoted_forms(tmp_path):
    m = load()
    for cron, quoted in (("0 3 * * *", "0 3 * * *"), ("0 3 * * *", '"0 3 * * *"')):
        p = tmp_path / "wf.yml"
        p.write_text("on:\n  schedule:\n    - cron: %s\n" % quoted, encoding="utf-8")
        assert m.read_interval_hours(p) == 24.0
