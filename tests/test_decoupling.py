"""Decoupling (add-data-stream-engine task 6.1): core runs without the host repo.

The OSS core must install and run with NO finddata workspace present: in-code
seed providers load, path-based legacy providers SKIP (with a reason) instead of
crashing import-catalog, and the worker image default is env-driven with a
neutral fallback (no internal registry host baked in).
"""
from __future__ import annotations

import os

import pytest

from fd_open_data_mcp.catalog import providers, importer


def test_finddata_root_or_none_absent(monkeypatch):
    """No FINDDATA_ROOT env + no marker dir → None (not a bogus path)."""
    monkeypatch.delenv("FINDDATA_ROOT", raising=False)
    # point the marker search at an empty tmp dir so nothing is found
    monkeypatch.setattr(providers, "_find_finddata_root",
                        lambda: __import__("pathlib").Path("/nonexistent-xyz"))
    assert providers.finddata_root_or_none() is None


def test_provider_available_in_code_seed():
    # cn-report / edgar / wbgapi are in-code seeds — always available
    for name in ("cn-report", "edgar", "wbgapi"):
        ok, _ = providers.provider_available(name)
        assert ok, name


def test_provider_available_path_based_absent(monkeypatch):
    # akshare needs the workspace; without it → not available, with a reason
    monkeypatch.delenv("FINDDATA_ROOT", raising=False)
    monkeypatch.setattr(providers, "_find_finddata_root",
                        lambda: __import__("pathlib").Path("/nonexistent-xyz"))
    ok, reason = providers.provider_available("akshare")
    assert ok is False
    assert "not present" in reason or "unresolvable" in reason


def test_import_all_skips_unavailable_not_crash(session, monkeypatch):
    """import_all with no workspace: path providers skipped, in-code seeds run,
    the whole call returns (does not raise)."""
    monkeypatch.delenv("FINDDATA_ROOT", raising=False)
    monkeypatch.setattr(providers, "_find_finddata_root",
                        lambda: __import__("pathlib").Path("/nonexistent-xyz"))
    reports = importer.import_all(session)
    skipped = [r for r in reports if r.get("status") == "skipped"]
    assert skipped, "expected some path providers skipped"
    # every skipped report carries a reason (loud, never silent)
    assert all("reason" in r for r in skipped)


def test_image_default_env_driven(session):
    """The cluster image default reads SCRAW_K8S_IMAGE; unset → neutral
    (no internal registry host). Python-side callable applies at flush."""
    from fd_open_data_mcp.models import Cluster
    os.environ.pop("SCRAW_K8S_IMAGE", None)
    c = Cluster(name="t", api_server="https://x:6443")
    session.add(c); session.flush()
    assert c.image is not None and "harbor.local" not in c.image
    os.environ["SCRAW_K8S_IMAGE"] = "my.registry/crawler:v1"
    c2 = Cluster(name="t2", api_server="https://x:6443")
    session.add(c2); session.flush()
    assert c2.image == "my.registry/crawler:v1"
