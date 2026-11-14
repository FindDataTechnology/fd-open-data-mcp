"""A rejected worker credential must be *named* as a credential problem.

`rotate-worker-cluster-credentials` 3.1 shipped a distinct exception type exactly so
a 401 from a worker cluster does not read as "launch failed": the two need opposite
reactions (rotate the credential vs. fix the plan). Nothing in the suite asserted
that, so a refactor could have collapsed it back into a generic failure with every
test still green.

2026-11-12: written in place of the "观察一个周期 / 线上镜像里 WorkerCredentialError
存在" acceptance step that the change's tasks asked for — user decision: there are no
real customers, the system keeps moving, and a constructed test is the durable judge
rather than one week of luck. What this cannot cover is stated plainly instead of
hidden: it proves the *code* attributes a 401 correctly; it does not prove the
running pods contain that code (that check is `__version__` + `import` inside the
image, recorded in tasks.md).
"""
from __future__ import annotations

import urllib.error

import pytest

from fd_open_data_mcp.refresh.reconciler import (
    ClusterK8sClient,
    WorkerCredentialError,
    worker_credential_hint,
)


class _Cluster:
    """Only the attributes ClusterK8sClient reads."""

    def __init__(self, name="cheap", api_server="https://api.cheap:6443",
                 namespace="scraw"):
        self.name = name
        self.api_server = api_server
        self.namespace = namespace


def _client(monkeypatch, token="forged-token"):
    """A client whose credential never touches disk — the point is the response."""
    monkeypatch.setattr(
        ClusterK8sClient, "_load",
        lambda self: (self.cluster.api_server, token, None),
    )
    return ClusterK8sClient(_Cluster())


def _deny(monkeypatch, code: int):
    def _urlopen(req, context=None, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError(req.full_url, code, "denied", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)


def test_rejected_token_is_named_a_credential_problem(monkeypatch):
    """401 → WorkerCredentialError, not a bare HTTPError the caller reads as 'launch failed'."""
    _deny(monkeypatch, 401)
    client = _client(monkeypatch)

    with pytest.raises(WorkerCredentialError) as ei:
        client.job_status("crawl-policy-1-x")

    msg = str(ei.value)
    assert "401" in msg
    assert "cheap" in msg
    # the operator needs the file to look at, not just the cluster name
    assert "$KUBECONFIG_DIR/cheap.token" in msg


def test_401_message_carries_a_runnable_rotation_command(monkeypatch):
    """The action must be copy-pasteable — a hint that says 'rotate it' is useless at 3am.

    This is the assertion that pins the 6.2 acceptance criterion ("用伪造 token 触发
    一次派发能在日志里看到轮换命令") to something executable.
    """
    _deny(monkeypatch, 401)
    client = _client(monkeypatch)

    with pytest.raises(WorkerCredentialError) as ei:
        client.job_status("crawl-policy-1-x")

    msg = str(ei.value)
    assert "python3 scripts/rotate_worker_credential.py --cluster cheap --apply" in msg


def test_403_prescribes_rbac_not_rotation(monkeypatch):
    """403 is 'credential valid, permission missing' — prescribing a rotation would send
    the operator to re-mint a credential that was never the problem."""
    _deny(monkeypatch, 403)
    client = _client(monkeypatch)

    with pytest.raises(WorkerCredentialError) as ei:
        client.job_status("crawl-policy-1-x")

    msg = str(ei.value)
    assert "403" in msg
    assert "RBAC" in msg
    assert "rotate_worker_credential" not in msg


def test_other_statuses_are_not_reclassified_as_credential_problems(monkeypatch):
    """The conversion must stay narrow: a 500 is a server problem, and calling it a
    credential problem would send the operator rotating something that works."""
    _deny(monkeypatch, 500)
    client = _client(monkeypatch)

    with pytest.raises(urllib.error.HTTPError) as ei:
        client.job_status("crawl-policy-1-x")

    assert not isinstance(ei.value, WorkerCredentialError)
    assert ei.value.code == 500


def test_hint_names_the_three_causes_of_a_dead_mounted_token():
    """Pure-function guard: the causes are what makes the hint actionable."""
    msg = worker_credential_hint("cheap", 401)
    for cause in ("过期", "SA", "摘除"):
        assert cause in msg, cause
