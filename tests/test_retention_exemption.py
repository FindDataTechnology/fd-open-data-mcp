"""豁免机制：超窗保留必须有理由、责任人与到期（spec 的一条 MUST）。

为什么必须测：这条规则最容易退化成"报告里打个标、谁也不看"。三条判据各堵一种退化：

1. 无豁免 → 超窗仍是违规（否则等于默认永久保留）；
2. 有效豁免 → 不再报违规（否则"我声明了却还红"，人就会去改判据而不是补豁免）；
3. 过期豁免 → 回到违规并点名（否则豁免成了永久豁免）。
"""

from datetime import date, timedelta

from scripts.audit_retention_contracts import Contract, classify_violations


def _contract(**kw) -> Contract:
    base = dict(source="akshare", family="price", cls="W", window_days=730,
                re_fetchable=True, upstream_history_mutable=False, budget_mb=0)
    base.update(kw)
    return Contract(**base)


def _violations(ct: Contract, *, over_window=100) -> list[str]:
    return classify_violations(ct, rows=1000, over_window=over_window,
                               rows_per_entity_recent=1.0, family=ct.family)


def test_over_window_without_exemption_is_a_violation():
    assert any("over_window" in v for v in _violations(_contract()))


def test_active_exemption_silences_the_over_window_violation():
    ct = _contract(exempt_reason="监管要求保留五年", exempt_owner="chengsishi",
                   exempt_until=date.today() + timedelta(days=30))
    assert not any("over_window" in v for v in _violations(ct)), \
        "有效豁免期内不该再报超窗违规——否则人只会去改判据"


def test_expired_exemption_returns_to_violation_and_names_the_owner():
    ct = _contract(exempt_reason="临时保留", exempt_owner="chengsishi",
                   exempt_until=date.today() - timedelta(days=1))
    hits = [v for v in _violations(ct) if "over_window" in v]
    assert hits, "过期豁免必须回到违规，否则豁免=永久保留"
    assert "豁免已失效" in hits[0] and "chengsishi" in hits[0], \
        f"要能回答'这条为什么还在、该找谁'，实际：{hits[0]}"


def test_exemption_state_helper():
    today = date(2026, 11, 11)
    assert _contract().exemption(today)[0] == "none"
    assert _contract(exempt_until=date(2026, 12, 1)).exemption(today)[0] == "active"
    assert _contract(exempt_until=date(2026, 11, 11)).exemption(today)[0] == "active"
    assert _contract(exempt_until=date(2026, 11, 10)).exemption(today)[0] == "expired"


def test_runtime_and_audit_exemption_implementations_agree():
    """运行期与审计器两份豁免实现必须说同一句话。

    为什么会有两份：审计器要能在**不装本包**的环境里跑（它是独立脚本，靠 importlib
    装载），所以各写一份纯函数。两份不比对，就会出现"审计说保留、写入说可删"——
    而这正是 2026-11-12 实测到的漏洞形态（豁免只对离线淘汰工具生效，两条写入路径
    根本不看它）。
    """
    from datetime import date as _d

    from fd_open_data_mcp.fetch.retention import exemption_state

    today = _d(2026, 11, 12)

    class Row:      # 模拟原始查询行 / ORM 行：属性取列
        def __init__(self, until, owner=None, reason=None):
            self.exempt_until, self.exempt_owner, self.exempt_reason = until, owner, reason

    cases = [
        (None, None, None),
        (_d(2026, 12, 1), "chengsishi", "监管要求保留五年"),
        (_d(2026, 11, 12), "chengsishi", "今天到期"),
        (_d(2026, 11, 11), "chengsishi", "昨天到期"),
        ("2026-12-01", "chengsishi", "字符串形态"),
    ]
    for until, owner, reason in cases:
        runtime = exemption_state(Row(until, owner, reason), today=today)
        audited = _contract(exempt_until=until, exempt_owner=owner,
                            exempt_reason=reason).exemption(today)
        assert runtime[0] == audited[0], \
            f"状态不一致 until={until!r}: 运行期={runtime[0]} 审计={audited[0]}"


def test_runtime_exemption_tolerates_a_row_without_the_column():
    """没跑 0021 的库没有 exempt_until 列——此时必须答"没有豁免"，不能炸也不能当有。"""
    from fd_open_data_mcp.fetch.retention import exemption_state

    class LegacyRow:
        retention_class = "W"
        retention_window_days = 1826

    assert exemption_state(LegacyRow()) == ("none", "")
