"""Launcher defaults must not carry a password, and must not put the DSN in a Job spec.

Two defects found 2026-11-11 while sweeping for literal credentials:

1. Both launchers defaulted to `postgres:<password>@fd-open-pg.scraw:5432/postgres`.
   That module ships in the PyPI sdist, so the password was *published*.
2. `K8sJobLauncher.launch` wrote `self.database_url` as a plain `value:` env entry,
   i.e. the real DSN (with its password) landed in every Job spec, readable with
   `get pods`. The multi-cluster launcher had already been fixed to use
   `fd-db-secret`; this one had not.
"""

import inspect

import pytest

from fd_open_data_mcp.refresh.reconciler import K8sJobLauncher, MultiClusterLauncher


def _clear(monkeypatch):
    for var in ("SCRAW_K8S_DATABASE_URL", "SCRAW_K8S_REDIS_URL"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("factory", [K8sJobLauncher, MultiClusterLauncher])
def test_no_password_in_the_default_dsn(monkeypatch, factory):
    """A published package must not carry a default credential.

    Asserted by shape (an `@` means userinfo, i.e. a password) rather than by
    naming the old value — the point is that no literal credential is there.
    """
    _clear(monkeypatch)
    launcher = factory()
    assert launcher.database_url is None, launcher.database_url
    assert launcher.redis_url is None, launcher.redis_url


@pytest.mark.parametrize("factory", [K8sJobLauncher, MultiClusterLauncher])
def test_env_is_still_honoured(monkeypatch, factory):
    """Removing the default must not break the documented override."""
    monkeypatch.setenv("SCRAW_K8S_DATABASE_URL", "postgresql+psycopg2://u:p@h:5432/d")
    monkeypatch.setenv("SCRAW_K8S_REDIS_URL", "redis://h:6379/0")
    launcher = factory()
    assert launcher.database_url.endswith("/d")
    assert launcher.redis_url.startswith("redis://h")


def test_k8s_launcher_gets_the_dsn_from_the_secret(monkeypatch):
    """No override -> secretKeyRef, never a literal value in the pod spec."""
    _clear(monkeypatch)
    env = {e["name"]: e for e in K8sJobLauncher()._db_env()}

    for name, key in (("FD_OPEN_DATA_MCP_DATABASE_URL", "DATABASE_URL"),
                      ("REDIS_URL", "REDIS_URL")):
        assert "value" not in env[name], env[name]
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "fd-db-secret", "key": key}


def test_k8s_launcher_keeps_an_explicit_override_literal(monkeypatch):
    """A deliberate local override stays a literal — that path has no Secret."""
    _clear(monkeypatch)
    launcher = K8sJobLauncher(database_url="postgresql+psycopg2://u:p@127.0.0.1:5432/d")
    env = {e["name"]: e for e in launcher._db_env()}

    assert env["FD_OPEN_DATA_MCP_DATABASE_URL"]["value"].endswith("/d")
    assert "valueFrom" not in env["FD_OPEN_DATA_MCP_DATABASE_URL"]


def test_launch_does_not_inline_the_dsn_any_more():
    """Guard the call site itself — the helper only helps if it is used."""
    src = inspect.getsource(K8sJobLauncher.launch)
    assert "self._db_env()" in src
    assert '"value": self.database_url' not in src
    assert '"value": self.redis_url' not in src
