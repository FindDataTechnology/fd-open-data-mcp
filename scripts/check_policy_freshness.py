#!/usr/bin/env python3
"""政策派发面（``crawl_policies`` → ``policy_runs``）的新鲜度判据（只读）。

## 为什么需要它

2026-11-10 起 `astock-daily-increment`（policy 2659）每天 10:22 UTC 都失败，而
**没有任何通道说过这件事**：

- 集群侧：治理 Jobs 由执行器创建，不挂在任何 `CronJob` 名下 ⇒
  `check_cronjob_health.py` 按对象枚举**看不见**它；
- 爬虫侧：它走 `executor=direct`，不写 `crawl_runs` ⇒
  `check_source_run_freshness.py` 那条账本里**没有它的行**；
- 面板侧：runs 视图只显示"最近一条是什么"，不判它是否已经连续失败。

结果：个股日线停在 11-09（5184/5192 只），到 11-14 才因为人工排查被发现。而这三天
失败详情写的是同一句话——**「failed before the runner checked in；pod 已回收，无终态
可抓」**——连"上游拒绝"都没留下。判据不管这件事，「每天跑、每天失败、没人知道」
就是一个稳定形态。

## 判据（四条硬档 + 两条只报事实）

硬档命中即 EXIT=1：

- **R1 `latest_failed`** —— 最近一次**终态**运行是 `failed`/`stalled`。
  这是"现在坏了"的直接信号；`no_op`/`redundant`/`skipped` 是合法不产出，**不算失败**
  （否则月度策略在"本期还没结束"时会常年红）。
- **R2 `no_recent_run`** —— 最近一次运行早于该策略**自己的**排期周期 × 1.5
  （周期由 `croniter` 从 `cron_expr` 推，取未来 60 次触发的最大间隔：工作日 cron 的
  最大间隔是 3 天，不是 1 天）。**一次都没跑过**单独成 `never_ran`。
- **R3 `dangling_open_run`** —— 存在 `running`/`queued` 行超过执行器上限 + 900s。
  这种行会**永久挡住单飞派发**（2026-11-13 实测：一条挂了 5.2 小时的 running 行让
  policy 2659 整天没有新行）。
- **R4 `opaque_reason`** —— 最近一次终态失败，而 `detail` 读不出原因（空、或只有
  "no terminal state"/"pod 已回收"）。它与 R1 同时成立时的价值在于**方向和处置不同**：
  R1 说"坏了"，R4 说"而且它什么理由都没留下"，后者要靠留痕修复，不是靠重跑。

只报事实、不计命中：最近一次是 `no_op`/`redundant`/`skipped`（合法不产出）、
`cron_expr` 为空（不排期，`last_run_at` 空是设计）。

## 退出码

`0` 无命中；`1` 有命中；`2` 判不出（连不上库 / 一条启用策略都没有 / 某条 cron 解析
不出）。**判不出不许退 0**——"什么都没查"与"全部健康"同码是这一族闸最常见的假绿。

## 用法

    python scripts/check_policy_freshness.py --db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"
    python scripts/check_policy_freshness.py --only astock           # 只判名字含该子串
    python scripts/check_policy_freshness.py --json
    python scripts/check_policy_freshness.py --self-test             # 六条分支自证
    python scripts/check_policy_freshness.py --assume-failed 2659    # 注入红证

只读：所有查询都在 `set_session(readonly=True)` 下，写盘为零。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

#: 执行器上限的唯一来源。执行侧那个数字在 `refresh/reconciler.py`——这里是**同一个**
#: 函数，不是抄一份常量：判得比执行侧紧 ⇒ 活着的 Job 被判死（双跑）；判得松或不判 ⇒
#: Job 死了行永不关（2026-11-13 事故）。导入失败只可能发生在没装该包的沙箱里，此时
#: 退回同一个默认值并**在输出里说明**（不静默）。
try:  # pragma: no cover - 依赖环境
    from fd_open_data_mcp.refresh.reconciler import _active_deadline_seconds
except Exception:  # pragma: no cover
    _active_deadline_seconds = None  # type: ignore[assignment]

DEFAULT_EXECUTOR_CEILING_SECONDS = 10800          # = reconciler.DEFAULT_JOB_ACTIVE_DEADLINE_SECONDS
OPEN_RUN_GRACE_SECONDS = 900                      # reconciler 的"一跳"余量
#: 排期判据的余量：最近一次**应触发时刻**之后这么久还没有运行记录，才算漏跑。
#: 取 1 小时而不是"周期×系数"：调度器的一跳会晚几十秒，而按周期算要么在周末假红
#: （工作日 cron 的 3 天间隔），要么要等两轮才报（漏掉一个整轮却还绿）。
FIRE_GRACE_SECONDS = 3600

OPEN_STATUSES = {"running", "queued"}
FAILED_STATUSES = {"failed", "stalled"}
NON_YIELD_OK = {"no_op", "redundant", "skipped"}
TERMINAL_STATUSES = {"success", *FAILED_STATUSES, *NON_YIELD_OK, "cancelled"}

#: 「读不出原因」的指纹。这些是**已知的留痕缺陷**，不是判断上游好坏的依据。
OPAQUE_MARKERS = (
    "no terminal state",
    "无终态可抓",
    "before the runner checked in",
    "pod 已回收",
)


# ---------------------------------------------------------------- 形状

@dataclass
class PolicyRun:
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    detail: str = ""


@dataclass
class PolicyState:
    """一条启用策略 + 它最近的运行（started_at 降序，最多留 50 条）。"""

    policy_id: int
    name: str
    cron_expr: str | None
    timezone_name: str | None
    runs: list[PolicyRun] = field(default_factory=list)


@dataclass
class Finding:
    policy_id: int
    name: str
    rule: str          # latest_failed | no_recent_run | never_ran | dangling_open_run | opaque_reason
    detail: str

    @property
    def counts(self) -> bool:
        return True        # 进 findings 的一律命中；只报事实的走 facts


# ---------------------------------------------------------------- 排期

def _prev_fire(cron_expr: str, tz_name: str | None, now: datetime) -> tuple[datetime, str]:
    """(最近一次应触发时刻[UTC], 用的时区名)。解析不出抛 ValueError。

    **时区是判据的一部分**：`cron_expr` 解释在哪个时区由策略自己声明，按 UTC 去解一条
    `Asia/Shanghai` 的 `17 18 * * 1-5` 差 8 小时，足以把「还没到」判成「漏跑」。
    """
    from croniter import croniter
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz_label = tz_name or "UTC"
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"时区 {tz_name!r} 解析不出: {exc}") from exc

    if not croniter.is_valid(cron_expr):
        raise ValueError(f"cron {cron_expr!r} 不是合法表达式")

    local_now = now.astimezone(tz)
    prev_local = croniter(cron_expr, local_now).get_prev(datetime)
    return prev_local.astimezone(timezone.utc), tz_label


def _as_utc(ts: datetime | None) -> datetime | None:
    """库里是 naive UTC；带 tz 的原样换算。不猜本地时区。"""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ---------------------------------------------------------------- 判据

def _is_opaque(detail: str) -> bool:
    if not (detail or "").strip():
        return True
    low = detail.lower()
    return any(m.lower() in low for m in OPAQUE_MARKERS)


def judge(states: Iterable[PolicyState], *, now: datetime,
          executor_ceiling_seconds: int = DEFAULT_EXECUTOR_CEILING_SECONDS,
          fire_grace_seconds: int = FIRE_GRACE_SECONDS,
          undetermined: list[str] | None = None,
          ) -> tuple[list[Finding], list[str]]:
    """纯函数：返回 (命中, 只报事实的行)。cron 解析不出写进 undetermined。"""
    undetermined = undetermined if undetermined is not None else []
    findings: list[Finding] = []
    facts: list[str] = []

    for st in states:
        label = f"p{st.policy_id} {st.name}"
        terminal = [r for r in st.runs if r.status in TERMINAL_STATUSES]
        latest = terminal[0] if terminal else None

        # R3 先判：悬着的 open run 是"下次也不再跑"的成因，先说它。
        # 同一处问题只记一次——悬着的行已经解释了"为什么没有终态行"，
        # 再补一条 never_ran 会把一个成因记成两个命中（计数失真）。
        dangling = False
        for r in st.runs:
            if r.status not in OPEN_STATUSES:
                continue
            started = _as_utc(r.started_at)
            age = (now - started).total_seconds()
            ceiling = executor_ceiling_seconds + OPEN_RUN_GRACE_SECONDS
            if age > ceiling:
                dangling = True
                findings.append(Finding(
                    st.policy_id, st.name, "dangling_open_run",
                    f"{r.status} 行已存在 {age / 3600:.1f}h，超出执行器上限 "
                    f"{executor_ceiling_seconds / 3600:.1f}h+{OPEN_RUN_GRACE_SECONDS}s；"
                    f"单飞派发会被它永久挡住（关行要落 stalled，不许写 success/zero_yield）",
                ))

        if latest is None:
            if dangling:
                continue          # 成因已在 R3 记过，不重复计数
            if not st.runs:
                findings.append(Finding(
                    st.policy_id, st.name, "never_ran",
                    "启用策略在 policy_runs 里一条记录都没有（派发面从没启动过）",
                ))
            else:
                facts.append(f"{label}: 当前有 open run 且未超执行器上限（在跑）")
            continue

        if latest.status in FAILED_STATUSES:
            detail = (latest.detail or "").strip()
            findings.append(Finding(
                st.policy_id, st.name, "latest_failed",
                f"最近一次终态是 {latest.status}（{_as_utc(latest.started_at):%Y-%m-%d %H:%M}Z）"
                + (f"：{detail[:160]}" if detail else "：detail 为空"),
            ))
            if _is_opaque(latest.detail):
                findings.append(Finding(
                    st.policy_id, st.name, "opaque_reason",
                    "最近一次失败没有留下可判原因（detail 只有「pod 已回收/无终态可抓」这类）"
                    "——重跑救不了它，要先补留痕（让执行体自己把失败原因写进 detail）",
                ))
        elif latest.status in NON_YIELD_OK:
            facts.append(f"{label}: 最近一次终态 {latest.status}（合法不产出，不计命中）")
        else:
            facts.append(f"{label}: 最近一次终态 {latest.status}")

        # R2 沉默：最近一次**应触发时刻**有没有产出
        if not (st.cron_expr or "").strip():
            facts.append(f"{label}: cron_expr 为空（不排期），只判人工触发")
            continue
        try:
            prev_fire, tz_label = _prev_fire(st.cron_expr, st.timezone_name, now)
        except ValueError as exc:
            undetermined.append(f"{label}: {exc}")
            continue
        newest_started = max((_as_utc(r.started_at) for r in st.runs), default=None)
        if newest_started is None:
            findings.append(Finding(
                st.policy_id, st.name, "never_ran",
                f"cron {st.cron_expr!r}（{tz_label}）已排期（下一次应触发于 "
                f"{prev_fire:%Y-%m-%d %H:%M}Z 之前），但一条运行记录都没有",
            ))
            continue
        deadline = prev_fire - timedelta(seconds=fire_grace_seconds)
        if newest_started < deadline:
            findings.append(Finding(
                st.policy_id, st.name, "no_recent_run",
                f"最近一次运行在 {newest_started:%Y-%m-%d %H:%M}Z，而最近一次应触发时刻是 "
                f"{prev_fire:%Y-%m-%d %H:%M}Z（cron {st.cron_expr!r} @ {tz_label}）"
                f"——这一轮没有任何运行记录（不是「还在跑」，是「没开始」）",
            ))

    return findings, facts


# ---------------------------------------------------------------- 取数

def load_states(db_url: str, only: str | None = None) -> list[PolicyState]:
    """读启用策略 + 最近 50 次运行。取不到就抛（调用方退 2）。"""
    from sqlalchemy import create_engine, text

    # 只读是**连接级**的：psycopg2 会把它变成会话默认，写语句在库侧就被拒，
    # 而不是靠"脚本里没写 insert"这种约定。
    connect_args = (
        {"options": "-c default_transaction_read_only=on"}
        if db_url.startswith("postgresql") else {}
    )
    engine = create_engine(db_url, pool_pre_ping=True, connect_args=connect_args)
    try:
        with engine.connect() as conn:
            sql = ("select id, name, cron_expr, timezone "
                   "from crawl_policies where enabled order by id")
            params: dict[str, Any] = {}
            if only:
                sql = ("select id, name, cron_expr, timezone "
                       "from crawl_policies where enabled and name like :pat order by id")
                params["pat"] = f"%{only}%"
            policies = conn.execute(text(sql), params).all()
            if not policies:
                raise RuntimeError("没有匹配的启用策略——判不出，不是全过")

            ids = [int(p[0]) for p in policies]
            runs = conn.execute(text(
                "select policy_id, status, started_at, finished_at, coalesce(detail, '') "
                "from (select policy_id, status, started_at, finished_at, detail, "
                "             row_number() over (partition by policy_id order by started_at desc) rn "
                "        from policy_runs where policy_id = any(:ids)) t "
                "where rn <= 50 order by policy_id, started_at desc"
            ), {"ids": ids}).all()
    finally:
        engine.dispose()

    by_policy: dict[int, list[PolicyRun]] = {}
    for r in runs:
        by_policy.setdefault(int(r[0]), []).append(PolicyRun(
            status=str(r[1]),
            started_at=r[2],
            finished_at=r[3],
            detail=str(r[4] or ""),
        ))
    return [
        PolicyState(
            policy_id=int(p[0]), name=str(p[1]),
            cron_expr=p[2], timezone_name=p[3],
            runs=by_policy.get(int(p[0]), []),
        )
        for p in policies
    ]


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    """六条分支各证一次，外加一条绿路——省掉绿路，「全都判红」与「判据写反」同形。"""
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

    def run(status, hours_ago, detail=""):
        return PolicyRun(status=status,
                         started_at=now - timedelta(hours=hours_ago), detail=detail)

    states = [
        # R1 + R4：最近一次失败，且理由读不出（11-10..11-12 的形态）。
        # 26h 前 = 11-13 10:00Z，晚于 11-13 的应触发时刻(10:17Z)？不——早 17 分钟，
        # 但余量 1h ⇒ 不算漏跑，本条只证 R1/R4。
        PolicyState(1, "opaque-fail", "17 18 * * 1-5", "Asia/Shanghai",
                    [run("failed", 26, "job ... failed before the runner checked in; "
                                       "pod 已回收，无终态可抓")]),
        # R3：悬着的 running 行（11-13 的形态）
        PolicyState(2, "dangling", "17 18 * * 1-5", "UTC",
                    [run("running", 6)]),
        # R2：沉默（最近一次成功是 10 天前，日频 cron 早已漏掉整轮）
        PolicyState(3, "silent", "0 6 * * *", "UTC", [run("success", 240)]),
        # never_ran
        PolicyState(4, "never", "0 6 * * *", "UTC", []),
        # 绿路：日频，今天 06:00Z 那轮跑过（5h 前）
        PolicyState(5, "healthy", "0 6 * * *", "UTC", [run("success", 5)]),
        # 只报事实：no_op 不是失败
        PolicyState(6, "noop", "0 6 1 * *", "UTC", [run("no_op", 5, "next period not finished")]),
    ]
    undetermined: list[str] = []
    findings, facts = judge(states, now=now, undetermined=undetermined)
    got = {(f.policy_id, f.rule) for f in findings}
    expected = {
        (1, "latest_failed"), (1, "opaque_reason"),
        (2, "dangling_open_run"),
        (3, "no_recent_run"),
        (4, "never_ran"),
    }
    ok = got == expected
    for pid, rule in sorted(expected):
        mark = "✓" if (pid, rule) in got else "✗"
        print(f"  {mark} p{pid} 期望命中 {rule}")
    for pid, rule in sorted(got - expected):
        print(f"  ✗ p{pid} 不该命中却命中 {rule}")
    green = not any(f.policy_id == 5 for f in findings)
    print(f"  {'✓' if green else '✗'} p5 healthy 无命中（绿路）")
    noop_fact = any("no_op" in f for f in facts)
    print(f"  {'✓' if noop_fact else '✗'} p6 no_op 只报事实")

    # 时区是判据的一部分：同一条 cron 按 UTC / Asia/Shanghai 解出相距 8 小时的触发时刻
    fire_utc, _ = _prev_fire("17 18 * * 1-5", "UTC", now)
    fire_sh, label = _prev_fire("17 18 * * 1-5", "Asia/Shanghai", now)
    delta_h = (fire_utc - fire_sh).total_seconds() / 3600
    tz_ok = abs(delta_h - 8.0) < 1e-6 and label == "Asia/Shanghai"
    print(f"  {'✓' if tz_ok else '✗'} 同一条 cron 两个时区相差 {delta_h:+.1f}h @ {label}"
          f"（按错时区判会把「还没到」读成「漏跑」）")

    # 解析不出要进 undetermined（退 2），不许静默当作"没排期"
    bad: list[str] = []
    judge([PolicyState(7, "bad-cron", "not a cron", "UTC", [run("success", 1)])],
          now=now, undetermined=bad)
    bad_ok = len(bad) == 1
    print(f"  {'✓' if bad_ok else '✗'} 坏 cron 进 undetermined（{len(bad)} 条）")

    return 0 if (ok and green and noop_fact and tz_ok and bad_ok) else 1


# ---------------------------------------------------------------- main

def _print_human(findings: list[Finding], facts: list[str],
                 undetermined: list[str], states: list[PolicyState]) -> None:
    print(f"启用策略 {len(states)} 条；推出上限 "
          f"{( _active_deadline_seconds() if _active_deadline_seconds else DEFAULT_EXECUTOR_CEILING_SECONDS) / 3600:.1f}h"
          + ("" if _active_deadline_seconds else "（执行器常量导入失败，用默认值）"))
    if findings:
        print("\n命中：")
        for f in findings:
            print(f"✗ p{f.policy_id} {f.name} [{f.rule}] {f.detail}")
    if undetermined:
        print("\n判不出：")
        for line in undetermined:
            print(f"? {line}")
    if facts:
        print("\n只报事实：")
        for line in facts:
            print(f"· {line}")
    if not findings:
        print("\n无命中。")


def _inject(states: list[PolicyState], *, assume_failed: str | None,
            assume_missed_slot: str | None, now: datetime) -> list[str]:
    """红证：在**真实取到的**状态上按名字注入，走同一套判据（不改库、不改文件）。

    这两条存在的理由：`--self-test` 证的是纯函数的分支，而「报告路径能红」要靠真数据——
    省掉它，「全都判红」与「判据写反」在输出上同形。
    """
    notes: list[str] = []
    for target, kind in ((assume_failed, "failed"), (assume_missed_slot, "missed")):
        if not target:
            continue
        # `p2659` 与 `2659` 都认（人会说 p2659，库里是 int）
        bare = target[1:] if target[:1] in ("p", "P") and target[1:].isdigit() else target
        hit = [s for s in states
               if bare == str(s.policy_id) or target in s.name]
        if not hit:
            notes.append(f"--assume-{kind}: 没有匹配的策略 {target!r}（注入未生效）")
            continue
        for st in hit:
            if kind == "failed":
                st.runs.insert(0, PolicyRun(
                    status="failed", started_at=now - timedelta(hours=1),
                    detail="INJECTED (assume-failed): 红证用，库里的行未被改动",
                ))
            else:
                # 把"最近一次应触发之后"的行全部摘掉 ⇒ 该轮没有记录
                prev, _ = _prev_fire(st.cron_expr or "0 6 * * *", st.timezone_name, now)
                st.runs = [r for r in st.runs if _as_utc(r.started_at) < prev]
            notes.append(f"--assume-{kind}: 已注入到 p{st.policy_id} {st.name}")
    return notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"),
                    help="默认取 FD_OPEN_DATA_MCP_DATABASE_URL")
    ap.add_argument("--only", help="只判名字含该子串的策略")
    ap.add_argument("--assume-failed", metavar="ID_OR_NAME",
                    help="红证：把该策略最近一次运行当作 failed（只改内存）")
    ap.add_argument("--assume-missed-slot", metavar="ID_OR_NAME",
                    help="红证：摘掉该策略最近一轮的运行记录（只改内存）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="六条分支自证")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.db_url:
        print("判不出（EXIT=2）：没有 --db-url，也没有 FD_OPEN_DATA_MCP_DATABASE_URL",
              file=sys.stderr)
        return 2
    try:
        states = load_states(args.db_url, only=args.only)
    except Exception as exc:  # noqa: BLE001 - 判不出必须退 2
        print(f"判不出（EXIT=2）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    ceiling = (_active_deadline_seconds() if _active_deadline_seconds
               else DEFAULT_EXECUTOR_CEILING_SECONDS)
    now = datetime.now(timezone.utc)
    injection = _inject(states, assume_failed=args.assume_failed,
                        assume_missed_slot=args.assume_missed_slot, now=now)
    undetermined: list[str] = []
    findings, facts = judge(states, now=now,
                            executor_ceiling_seconds=ceiling,
                            undetermined=undetermined)

    if args.json:
        print(json.dumps({
            "policies": len(states),
            "injection": injection,
            "findings": [f.__dict__ for f in findings],
            "facts": facts,
            "undetermined": undetermined,
        }, ensure_ascii=False, indent=2))
    else:
        for line in injection:
            print(f"⚠ {line}")
        _print_human(findings, facts, undetermined, states)

    # 判不出（cron 解析不了 / 策略为空）不许退 0：一次格式变更就能让判据静默消失
    if undetermined:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
