"""`_read_script` must not silently pick between two different copies.

Measured 2026-11-11 on fd-master: the mounted `fd-scripts` ConfigMap is a
2026-08-28 snapshot and still holds five `bulk_ingest_*.py` that differ from the
ones shipped inside the image. Resolution order is /app/scripts first, so the
current copy was used — but the duplicated, stale copy was invisible. Whoever
edited only the ConfigMap (or only the repo, without cutting a release) would
have seen no effect and no error.
"""
import pytest

from fd_open_data_mcp.refresh import reconciler


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    a = tmp_path / "app_scripts"
    b = tmp_path / "configmap"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(reconciler, "_SCRIPT_SEARCH", None, raising=False)
    # 只改 env：search 列表里前两项是写死的绝对路径，测试里换成 tmp 目录
    monkeypatch.setenv("FD_SCRIPTS_DIR", str(b))
    return a, b


def _read_from(monkeypatch, a, b):
    """用一份只有 tmp 目录的 search 列表调用真实实现。"""
    import builtins, os
    orig_isfile = os.path.isfile

    def fake_isfile(p):
        # 把两处写死的镜像路径都指向 tmp 的 a 目录
        for hard in ("/app/scripts", "/opt/fd-scripts"):
            if p.startswith(hard + os.sep):
                return orig_isfile(os.path.join(str(a), os.path.basename(p)))
        return orig_isfile(p)

    monkeypatch.setattr(os.path, "isfile", fake_isfile)
    orig_open = builtins.open

    def fake_open(p, *arg, **kw):
        for hard in ("/app/scripts", "/opt/fd-scripts"):
            if str(p).startswith(hard + os.sep):
                return orig_open(os.path.join(str(a), os.path.basename(p)), *arg, **kw)
        return orig_open(p, *arg, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    return reconciler._read_script("/whatever/x.py")


def test_identical_copies_stay_quiet(dirs, monkeypatch, caplog):
    a, b = dirs
    (a / "x.py").write_text("print('same')\n", encoding="utf-8")
    (b / "x.py").write_text("print('same')\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        src = _read_from(monkeypatch, a, b)

    assert "same" in src
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_differing_copy_is_named(dirs, monkeypatch, caplog):
    a, b = dirs
    (a / "x.py").write_text("print('image')\n", encoding="utf-8")
    (b / "x.py").write_text("print('configmap-old')\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        src = _read_from(monkeypatch, a, b)

    # 行为不变：仍然用第一处
    assert "image" in src
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn) == 1, warn
    # 三件事都要说出来：哪个脚本、从哪取的、谁被遮住
    assert "x.py" in warn[0]
    assert "/app/scripts" in warn[0]      # 报的是查找路径，运维才认得出
    assert str(b) in warn[0]


def test_missing_script_still_raises(dirs, monkeypatch):
    a, b = dirs
    with pytest.raises(FileNotFoundError):
        _read_from(monkeypatch, a, b)


# ── 别名（_v2）这一层：2026-11-15 的线上形态 ────────────────────────────────
# 上面三条用 fake open 把两处镜像路径都指到同一个目录，因此**分不出**
# 「镜像 vs ConfigMap」这一级——而 2026-11-15 真正踩到的就是这一级：镜像里
# 有旧的 `bulk_ingest_astock_daily.py`（52336 B），ConfigMap 里是新修的
# `..._v2.py`（66414 B），策略改的正是 `_v2` 这个名字。两条用例成对写：
# 别名必须落到 ConfigMap；同名（非别名）必须被镜像遮住 —— 只写前者的话，
# 「查找顺序整体反了」也能全绿。


@pytest.fixture
def three_dirs(tmp_path, monkeypatch):
    """镜像目录 / ConfigMap 目录 / FD_SCRIPTS_DIR 三个互不相同的真实目录。"""
    image = tmp_path / "app_scripts"
    configmap = tmp_path / "opt_fd_scripts"
    extra = tmp_path / "fd_scripts_dir"
    for d in (image, configmap, extra):
        d.mkdir()
    monkeypatch.setattr(reconciler, "_SCRIPT_SEARCH_DIRS", (str(image), str(configmap)))
    monkeypatch.setenv("FD_SCRIPTS_DIR", str(extra))
    return image, configmap, extra


def test_alias_name_falls_through_to_the_mounted_configmap(three_dirs, caplog):
    """镜像里没有 `_v2` ⇒ 命中 CM 那份，且**不**报「被遮住」（没有第二份）。"""
    image, configmap, _ = three_dirs
    (image / "bulk_ingest_astock_daily.py").write_text("OLD image copy\n", encoding="utf-8")
    (configmap / "bulk_ingest_astock_daily_v2.py").write_text("NEW fixed copy\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        src = reconciler._read_script("/app/scripts/bulk_ingest_astock_daily_v2.py")

    assert src == "NEW fixed copy\n"
    assert not [r for r in caplog.records if r.levelname == "WARNING"], \
        "只有一个来源时不许报警告"


def test_plain_name_gets_the_image_copy_so_the_alias_is_what_protects_you(three_dirs, caplog):
    """同一棵树里问非别名：拿到的必须是**镜像**那份（旧代码），并点名被遮的 CM。

    与上一条成对：它证明「`_v2` 生效」不是因为顺序整体反了，而是因为别名在
    镜像里不存在。策略哪天被改回非别名，跑的就会是这段旧代码。
    """
    image, configmap, _ = three_dirs
    (image / "bulk_ingest_astock_daily.py").write_text("OLD image copy\n", encoding="utf-8")
    (configmap / "bulk_ingest_astock_daily.py").write_text("NEW fixed copy\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        src = reconciler._read_script("/app/scripts/bulk_ingest_astock_daily.py")

    assert src == "OLD image copy\n"
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn) == 1, warn
    assert str(configmap) in warn[0]


def test_fd_scripts_dir_is_the_last_resort(three_dirs):
    """前两处都没有时才用 FD_SCRIPTS_DIR（控制面 pod 不装 repo 的场景）。"""
    _, _, extra = three_dirs
    (extra / "only_here.py").write_text("from env dir\n", encoding="utf-8")

    assert reconciler._read_script("/whatever/only_here.py") == "from env dir\n"
