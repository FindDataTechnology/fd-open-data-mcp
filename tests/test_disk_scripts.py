"""两个存储运维脚本的契约测试（expand_root_fs.sh / check_disk_headroom.sh）。

这两个脚本会动磁盘，所以本文件只测**判定逻辑与退出码**，不测真扩容：
  * 阈值判定必须来自真实 df 数值，不能恒真；
  * 读不到就是未知（退出 2），不能当"正常"（退出 0）——这是本项目反复踩过的坑：
    "探针没跑"和"探针通过"长得一模一样。
  * expand 脚本在非 root / 设备不存在 / 磁盘无空闲空间时必须拒绝，且绝不误判成功。
"""

import os
import re
import shutil
import subprocess
import pathlib

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
HEADROOM = SCRIPTS / "check_disk_headroom.sh"
EXPAND = SCRIPTS / "expand_root_fs.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")


def run(script, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=e, timeout=60,
    )


# --- check_disk_headroom.sh -------------------------------------------------

def test_headroom_ok_on_temp_dir(tmp_path):
    """临时目录空间充裕 → 退出 0。"""
    r = run(HEADROOM, env={"MOUNTS": str(tmp_path), "THRESHOLD_MB": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout


def test_headroom_trips_when_threshold_absurdly_high(tmp_path):
    """阈值设成天文数字 → 必须退出 1，而不是继续报 OK。

    这是"判据恒真"的反向守卫：如果实现里把阈值写死或漏比较，这条会红。
    """
    r = run(HEADROOM, env={"MOUNTS": str(tmp_path), "THRESHOLD_MB": "999999999"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "LOW" in r.stdout


def test_headroom_low_is_real_number_not_constant(tmp_path):
    """LOW 行里必须带出真实可用量，且确实小于阈值。"""
    r = run(HEADROOM, env={"MOUNTS": str(tmp_path), "THRESHOLD_MB": "999999999"})
    m = re.search(r"可用 (\d+) MiB", r.stdout)
    assert m, r.stdout
    assert int(m.group(1)) < 999999999


def test_headroom_unknown_mount_is_not_success():
    """挂载点不存在 ⇒ 未知 ⇒ 退出 2。未知绝不能读成正常。"""
    r = run(HEADROOM, env={"MOUNTS": "/definitely/not/a/mount", "THRESHOLD_MB": "1"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "UNKNOWN" in r.stderr


def test_headroom_warn_only_still_zeros_the_exit(tmp_path):
    r = run(HEADROOM, "--warn-only",
            env={"MOUNTS": str(tmp_path), "THRESHOLD_MB": "999999999"})
    assert r.returncode == 0
    assert "LOW" in r.stdout


def test_headroom_help_does_not_touch_disk():
    r = run(HEADROOM, "--help")
    assert r.returncode == 0
    assert "check_disk_headroom" in r.stdout


# --- expand_root_fs.sh ------------------------------------------------------

def test_expand_refuses_without_root():
    """非 root 必须拒绝（本机以普通用户跑；CI 里跑 root 就跳过）。"""
    if os.geteuid() == 0:
        pytest.skip("以 root 运行，无法验证拒绝分支")
    r = run(EXPAND, env={"DEVICE": "/dev/vda"})
    assert r.returncode == 1
    assert "需要 root" in r.stderr


def test_expand_refuses_missing_device():
    """设备不存在时必须报"不是块设备"，不能继续。

    注意顺序：root 门槛在设备门槛之前（先确认有权动盘，再谈盘在哪）。
    所以这条只有在 root 下才走得到设备分支；非 root 时跳过，另用静态断言补位。
    """
    if os.geteuid() != 0:
        pytest.skip("非 root 会先被 root 门槛拦住，走不到设备检查分支")
    r = run(EXPAND, env={"DEVICE": "/dev/definitely-not-a-disk"})
    assert r.returncode == 1
    assert "不是块设备" in r.stderr


def test_expand_has_device_guard_before_any_change():
    """静态补位：设备存在性检查必须在 growpart 之前。"""
    src = EXPAND.read_text()
    guard = src.index('[ -b "$DEVICE" ]')
    grow = src.index('growpart "$DEVICE" "$PARTNUM"')
    assert guard < grow


def test_expand_dry_run_is_default():
    """源码层面确认：没有 --apply 时不会调用 growpart / resize。"""
    src = EXPAND.read_text()
    # APPLY 默认 0，且执行段在 APPLY 判断之后
    assert re.search(r"^APPLY=0$", src, re.M)
    guard = src.index('if [ "$APPLY" -ne 1 ]')
    grow = src.index("growpart \"$DEVICE\" \"$PARTNUM\"")
    assert guard < grow, "dry-run 判断必须早于 growpart 调用"


def test_expand_has_safety_gates():
    """三个硬门槛必须在：末尾分区、只读挂载、文件系统类型。"""
    src = EXPAND.read_text()
    assert "不是最后一个分区" in src
    assert "只读挂载" in src
    assert "不支持的文件系统" in src


def test_expand_unknown_fstype_is_not_silent():
    """查不到文件系统类型时必须退出，而不是默认按 ext4 处理。"""
    src = EXPAND.read_text()
    assert "未知不等于安全" in src


def test_scripts_are_syntactically_valid():
    """bash -n：语法错误在部署时才暴露的代价太高。"""
    for s in (HEADROOM, EXPAND):
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{s.name}: {r.stderr}"
