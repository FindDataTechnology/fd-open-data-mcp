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
