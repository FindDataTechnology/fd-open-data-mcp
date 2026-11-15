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

## 判据（五条硬档 + 两条只报事实）

硬档命中即 EXIT=1：

- **R0 `unknown_status`（判不出）** —— 最新一跳的状态不在这道闸认识的集合里。
  不认识的状态既不能当成功也不能当失败（`policy_runs` 里出现过 `aborted` 173 行，
  仓内只有 wave 侧语义、没有 run 侧语义），所以**整条策略判不出**，进 EXIT=2。
  这一条是结构性的：没有它，下一个新状态会像 `zero_yield` 一样静默走绿路。
- **R1 `latest_failed`** —— 最近一次**终态**运行是 `failed`/`stalled`。
  这是"现在坏了"的直接信号；`no_op`/`redundant`/`skipped` 是合法不产出，**不算失败**
  （否则月度策略在"本期还没结束"时会常年红）。
- **R1b `repeated_zero_yield`** —— 最近 **连续 ≥3 次**终态都是 `zero_yield`。
  生产者把这个状态叫 **the outage shape**：计划里有格子、却一行都没抓到，或连一次
  flush 都没上报。它与 R1 分开的理由是处置不同——**没有任何报错**、job 每次成功退出、
  日志里只有一次正常结束，要看的是上游还在不在给数据。
  取 3 次而不是 1 次：单次零产出在周末/长假/上游迟发时都会出现，逐次报警会得到一道
  每逢周末必红的闸；而周末连着两天只有 2 次，两天永远不够。
- **R2 `no_recent_run`** —— 最近一次运行早于该策略**自己的**排期周期 × 1.5
  （周期由 `croniter` 从 `cron_expr` 推，取未来 60 次触发的最大间隔：工作日 cron 的
  最大间隔是 3 天，不是 1 天）。**一次都没跑过**单独成 `never_ran`。
  另有拉起宽限（`PICKUP_GRACE_SECONDS`）：cron 到点 ≠ 立即执行——direct 策略要等
  下一轮 reconciler（每 15 分钟）才建 Job，2026-11-15 实测 06:45Z 的 tick 在 06:52Z
  才出现 running 行 ⇒ 最近一个 fire 还在拉起窗口内时改判上一个 fire，否则 cron 靠近
  闸运行时刻的策略会得到假红。
- **R3 `dangling_open_run`** —— 存在 `running`/`queued` 行超过执行器上限 + 900s。
  这种行会**永久挡住单飞派发**（2026-11-13 实测：一条挂了 5.2 小时的 running 行让
  policy 2659 整天没有新行）。
- **R4 `opaque_reason`** —— 最近一次终态失败，而 `detail` 读不出原因（空、或只有
  "no terminal state"/"pod 已回收"）。它与 R1 同时成立时的价值在于**方向和处置不同**：
  R1 说"坏了"，R4 说"而且它什么理由都没留下"，后者要靠留痕修复，不是靠重跑。

只报事实、不计命中：最近一次是 `no_op`/`redundant`/`skipped`（合法不产出）、
`zero_yield` 但未连续 3 次（单次形态，会连成因一起打印）、
`cron_expr` 为空（不排期，`last_run_at` 空是设计）。

## 退出码

`0` 无命中；`1` 有命中；`2` 判不出（连不上库 / 一条启用策略都没有 / 某条 cron 解析
不出 / 最新一跳是未知状态）。**判不出不许退 0**——"什么都没查"与"全部健康"同码是
这一族闸最常见的假绿。

## 用法

    python scripts/check_policy_freshness.py --db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"
    python scripts/check_policy_freshness.py --only astock           # 只判名字含该子串
    python scripts/check_policy_freshness.py --json
    python scripts/check_policy_freshness.py --self-test             # 九条分支自证
    python scripts/check_policy_freshness.py --assume-failed 2659    # 注入红证
    python scripts/check_policy_freshness.py --assume-zero-yield 2   # 注入零产出红证

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
#: cron 到点 ≠ 立即执行：direct 策略的 Job 由 `crawl-reconciler` 每 15 分钟一轮
#: （`7,22,37,52 * * * *`）现建，到点后要等下一轮 reconciler 才有运行记录。
#: 2026-11-15 实测（policy 3，cron `45 6 * * *`）：06:45Z 的 tick 在 06:52:04Z 才出现
#: `running` 行，而 06:50Z 跑这道闸会得到一条假 `no_recent_run`。取 30 分钟＝两轮
#: reconciler；在这个窗口内 R2 改判**上一个** fire——它的拉起窗口必定已经关闭。
PICKUP_GRACE_SECONDS = 1800

OPEN_STATUSES = {"running", "queued"}
FAILED_STATUSES = {"failed", "stalled"}
#: 生产者自己把这个状态叫 **the outage shape**（`reconciler.classify_yield`）：计划里有
#: 格子，却一行都没抓到，或者连一次 flush 都没上报。它**不是**「合法不产出」——
#: `no_op`（计划为空）与 `redundant`（抓到了但都已在库）才是。
#:
#: 2026-11-15 量到：这个状态此前**不在这道闸的任何一档里**（既不在 FAILED、也不在
#: TERMINAL），于是每条策略的「最近一次终态」都跳过它、读到上一次 success。实况：
#: policy 2 `fund-pilot-aum` 在 11-08/09/10 连出三次 zero_yield，而这三天的闸输出
#: 写的是「最近一次终态 success」——生产者已经把「供数中断」记进账本了，读的人却
#: 看不见。面板 cockpit 同期已把它算进 `failures`，这道闸是那一圈里唯一的漏档。
ZERO_YIELD_STATUS = "zero_yield"
#: 零产出按**连续次数**判，不按单次：单次「计划了却没抓到」在周末/长假/上游迟发时
#: 都会出现，逐次报警会得到一道每逢周末必红的闸（本仓已有结论：常年红的闸会被无视）。
#: 取 3 而不是 2：周末连着两天是常态形态，两天永不够；真断供到第三天就红。
ZERO_YIELD_STREAK_LIMIT = 3
NON_YIELD_OK = {"no_op", "redundant", "skipped"}
TERMINAL_STATUSES = {"success", *FAILED_STATUSES, *NON_YIELD_OK, "cancelled",
                     ZERO_YIELD_STATUS}
#: 认识的全部状态。**不认识的状态出现在最新一跳 ⇒ 判不出**，不许静默走进绿路。
#: 2026-11-15 量到 policy_runs 里另有 `aborted`（173 行，全在 08-30..09-18，只有 1 行
#: 属于启用策略且是 09-18 的老行），仓里没有一处定义它在 run 侧的语义（`expander` 里
#: 那个是 wave 状态）。猜一个含义去归类，等于替未定义的语义背书，所以只让它变成
#: 「判不出」，不变成绿也不变成红。
KNOWN_STATUSES = OPEN_STATUSES | TERMINAL_STATUSES

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
    #: `classify_yield` 就是拿这三列把一次"job 成功"的跑批分成 no_op / zero_yield /
    #: redundant / success。少了它们，零产出只能报「零产出」，说不清是「pod 在首次
    #: 上报前就死了」还是「抓了但上游没给」——而这两件事的处置完全不同。
    plan_cells: int | None = None
    rows_attempted: int | None = None
    rows_new: int | None = None


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
    rule: str          # latest_failed | repeated_zero_yield | no_recent_run | never_ran
                       # | dangling_open_run | opaque_reason
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


def _zero_yield_shape(run: PolicyRun) -> str:
    """零产出的两种成因，按 `reconciler.classify_yield` 的分支逐字对齐。

    这两句必须分开写：`attempted is None` = pod 在第一次上报前就结束了（"—它压根没跑
    到抓数那一步"），`attempted == 0` = 跑了、上游一行都没给（"供数中断"）。前者去查
    pod 为什么早退，后者去查上游为什么空返回——写成同一句会让人查错方向。
    """
    planned = run.plan_cells
    plan_txt = f"计划 {planned} 格" if planned is not None else "计划格数未记录"
    if run.rows_attempted is None or run.rows_new is None:
        return f"{plan_txt}，但连一次 flush 都没上报（pod 在首次上报前就结束了）"
    if run.rows_attempted == 0:
        return f"{plan_txt}，抓取 0 行（上游没有给数据 = 供数中断的形态）"
    return f"{plan_txt}，抓了 {run.rows_attempted} 行却没落地新行"


def judge(states: Iterable[PolicyState], *, now: datetime,
          executor_ceiling_seconds: int = DEFAULT_EXECUTOR_CEILING_SECONDS,
          fire_grace_seconds: int = FIRE_GRACE_SECONDS,
          pickup_grace_seconds: int = PICKUP_GRACE_SECONDS,
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

        # R0 未知状态：最新一跳的状态不在认识的集合里 ⇒ 判不出。
        # 放在所有判据之前，因为它的存在让「最近一次终态」这个说法本身失效——我们不
        # 知道那一跳算不算终态、算成功还是失败。**不许落到绿路**：那正是这道闸在
        # 2026-11-15 之前对 zero_yield 犯的错（不认识的状态静默读成上一次成功）。
        newest_any = st.runs[0] if st.runs else None
        if newest_any is not None and newest_any.status not in KNOWN_STATUSES:
            undetermined.append(
                f"{label}: 最新一跳状态 {newest_any.status!r} 不在认识的状态集里"
                f"（{_as_utc(newest_any.started_at):%Y-%m-%d %H:%M}Z）——判不出它是成功、"
                f"失败还是不产出；先给它一个语义再判"
            )
            continue

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
        elif latest.status == ZERO_YIELD_STATUS:
            # R1b 零产出：`zero_yield` 是「跑了，但一行新数据都没有」。与 `latest_failed`
            # 分开，因为处置不同——这里没有报错、没有异常堆栈、job 是成功退出的，翻日志
            # 只会看到一次正常结束；要看的是上游到底给没给数据。
            streak = 0
            for r in terminal:
                if r.status != ZERO_YIELD_STATUS:
                    break
                streak += 1
            shape = _zero_yield_shape(latest)
            if streak >= ZERO_YIELD_STREAK_LIMIT:
                findings.append(Finding(
                    st.policy_id, st.name, "repeated_zero_yield",
                    f"最近 {streak} 次终态全是零产出（最近一次 "
                    f"{_as_utc(latest.started_at):%Y-%m-%d %H:%M}Z）：{shape}"
                    f"——连续 {ZERO_YIELD_STREAK_LIMIT} 次以上就不是「恰好没有新数据」，"
                    f"去看上游是否还在给数据（这条没有报错，job 每次都成功退出）",
                ))
            else:
                facts.append(f"{label}: 最近一次终态 zero_yield（{shape}）"
                             f"——未连续 {ZERO_YIELD_STREAK_LIMIT} 次，只报事实")
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
        # 拉起宽限：cron 到点 ≠ 立即执行，要等下一轮 reconciler（见 PICKUP_GRACE_SECONDS）。
        # 最近一个 fire 还在拉起窗口内时改判上一个 fire——它对应的窗口必定已经关闭。
        if (now - prev_fire).total_seconds() < pickup_grace_seconds:
            prev_fire, tz_label = _prev_fire(st.cron_expr, st.timezone_name,
                                             prev_fire - timedelta(seconds=1))
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
                "select policy_id, status, started_at, finished_at, coalesce(detail, ''), "
                "       plan_cells, rows_attempted, rows_new "
                "from (select policy_id, status, started_at, finished_at, detail, "
                "             plan_cells, rows_attempted, rows_new, "
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
            plan_cells=r[5],
            rows_attempted=r[6],
            rows_new=r[7],
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
    """九条分支各证一次，外加两条绿路——省掉绿路，「全都判红」与「判据写反」同形。"""
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

    def run(status, hours_ago, detail="", *, cells=None, att=None, new=None):
        return PolicyRun(status=status,
                         started_at=now - timedelta(hours=hours_ago), detail=detail,
                         plan_cells=cells, rows_attempted=att, rows_new=new)

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
        # R1b：三次连续零产出、计划了格子却抓 0 行 —— 2026-11-08..11-10 policy 2 的形态。
        # 此前这一串在这道闸里完全不可见（latest 跳过 zero_yield 取到上一次 success）。
        PolicyState(7, "outage", "0 6 * * *", "UTC", [
            run("zero_yield", 5, cells=630, att=0, new=0),
            run("zero_yield", 29, cells=630, att=0, new=0),
            run("zero_yield", 53, cells=630, att=0, new=0),
            run("success", 77, cells=630, att=630, new=630),
        ]),
        # 单次零产出只报事实（周末/长假形态）：周末连着两天也不该红，故取 2 次向下证
        PolicyState(8, "weekend", "0 6 * * *", "UTC", [
            run("zero_yield", 5, cells=2, att=0, new=0),
            run("zero_yield", 29, cells=2, att=0, new=0),
            run("success", 53, cells=2, att=2, new=2),
        ]),
        # R0：最新一跳的状态不在认识集里（`aborted` 就是这种：仓里只有 wave 侧语义）
        PolicyState(9, "unknown-status", "0 6 * * *", "UTC", [
            run("aborted", 5),
            run("success", 29),
        ]),
    ]
    undetermined: list[str] = []
    findings, facts = judge(states, now=now, undetermined=undetermined)
    got = {(f.policy_id, f.rule) for f in findings}
    expected = {
        (1, "latest_failed"), (1, "opaque_reason"),
        (2, "dangling_open_run"),
        (3, "no_recent_run"),
        (4, "never_ran"),
        (7, "repeated_zero_yield"),
    }
    ok = got == expected
    for pid, rule in sorted(expected):
        mark = "✓" if (pid, rule) in got else "✗"
        print(f"  {mark} p{pid} 期望命中 {rule}")
    for pid, rule in sorted(got - expected):
        print(f"  ✗ p{pid} 不该命中却命中 {rule}")
    green = not any(f.policy_id in (5, 8) for f in findings)
    print(f"  {'✓' if green else '✗'} p5 healthy / p8 两次零产出 无命中（绿路）")
    noop_fact = any("no_op" in f for f in facts)
    print(f"  {'✓' if noop_fact else '✗'} p6 no_op 只报事实")
    zy_fact = any("p8 weekend" in f and "zero_yield" in f for f in facts)
    print(f"  {'✓' if zy_fact else '✗'} p8 单/两次零产出虽不命中，但必须出现在「只报事实」里")
    # 零产出的两种成因要分开点名：查 pod 早退 vs 查上游空返回
    shape = _zero_yield_shape(PolicyRun("zero_yield", now, plan_cells=630, rows_attempted=0, rows_new=0))
    shape_died = _zero_yield_shape(PolicyRun("zero_yield", now, plan_cells=630))
    split_ok = "上游没有给数据" in shape and "首次上报前就结束" in shape_died
    print(f"  {'✓' if split_ok else '✗'} 零产出的两种成因各自点名（抓 0 行 ≠ pod 早退）")
    # 未知状态必须判不出，而不是静默当成"上一次成功"
    und_ok = any("p9 unknown-status" in u for u in undetermined)
    print(f"  {'✓' if und_ok else '✗'} p9 未知状态 aborted 进 undetermined（{len(undetermined)} 条）")

    # 时区是判据的一部分：同一条 cron 按 UTC / Asia/Shanghai 解出相距 8 小时的触发时刻
    fire_utc, _ = _prev_fire("17 18 * * 1-5", "UTC", now)
    fire_sh, label = _prev_fire("17 18 * * 1-5", "Asia/Shanghai", now)
    delta_h = (fire_utc - fire_sh).total_seconds() / 3600
    tz_ok = abs(delta_h - 8.0) < 1e-6 and label == "Asia/Shanghai"
    print(f"  {'✓' if tz_ok else '✗'} 同一条 cron 两个时区相差 {delta_h:+.1f}h @ {label}"
          f"（按错时区判会把「还没到」读成「漏跑」）")

    # 解析不出要进 undetermined（退 2），不许静默当作"没排期"
    bad: list[str] = []
    judge([PolicyState(10, "bad-cron", "not a cron", "UTC", [run("success", 1)])],
          now=now, undetermined=bad)
    bad_ok = len(bad) == 1
    print(f"  {'✓' if bad_ok else '✗'} 坏 cron 进 undetermined（{len(bad)} 条）")

    # 拉起宽限两臂（2026-11-15 实况复刻）：cron `45 6 * * *`，tick 06:45Z，上一轮运行
    # 06:52Z（= reconciler 那一轮拉起）。两臂只换「闸运行在 tick 后几分钟」：
    #   06:50Z（< 30min 拉起窗口）⇒ 改判上一个 fire（06:45 前一天的那轮，有运行）⇒ **绿**；
    #   08:50Z（窗口已关）⇒ 06:45 那轮确实没有运行 ⇒ **红**。
    # 少了绿臂，「把 R2 整个删掉」也能全绿；少了红臂，「宽限 24h」也全绿。
    gate_run = PolicyRun("success", datetime(2026, 3, 14, 6, 52, tzinfo=timezone.utc))
    f_open, _ = judge([PolicyState(11, "pickup-window", "45 6 * * *", "UTC", [gate_run])],
                      now=datetime(2026, 3, 15, 6, 50, tzinfo=timezone.utc),
                      undetermined=[])
    f_closed, _ = judge([PolicyState(12, "pickup-expired", "45 6 * * *", "UTC", [gate_run])],
                        now=datetime(2026, 3, 15, 8, 50, tzinfo=timezone.utc),
                        undetermined=[])
    open_ok = not any(f.policy_id == 11 for f in f_open)
    closed_ok = any(f.policy_id == 12 and f.rule == "no_recent_run" for f in f_closed)
    print(f"  {'✓' if open_ok else '✗'} tick 后 5 分钟跑闸 ⇒ 不报 no_recent_run（拉起宽限内改判上一个 fire）")
    print(f"  {'✓' if closed_ok else '✗'} tick 后 2h5m 跑闸 ⇒ 照报 no_recent_run（宽限不吞真漏跑）")

    return 0 if (ok and green and noop_fact and zy_fact and split_ok and und_ok
                 and tz_ok and bad_ok) else 1


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
            assume_missed_slot: str | None, now: datetime,
            assume_zero_yield: str | None = None) -> list[str]:
    """红证：在**真实取到的**状态上按名字注入，走同一套判据（不改库、不改文件）。

    这三条存在的理由：`--self-test` 证的是纯函数的分支，而「报告路径能红」要靠真数据——
    省掉它，「全都判红」与「判据写反」在输出上同形。

    `--assume-zero-yield` 注入的是**连续 3 次**，不是 1 次：判据本身按连续次数计，
    只注一次什么也不会发生（那正是它该有的行为），而那样做红证会「失败得像成功」。
    """
    notes: list[str] = []
    for target, kind in ((assume_failed, "failed"), (assume_missed_slot, "missed"),
                         (assume_zero_yield, "zero-yield")):
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
            elif kind == "zero-yield":
                need = ZERO_YIELD_STREAK_LIMIT
                injected = [
                    PolicyRun(
                        status=ZERO_YIELD_STATUS, started_at=now - timedelta(hours=1 + 24 * i),
                        detail="INJECTED (assume-zero-yield): 红证用，库里的行未被改动",
                        plan_cells=630, rows_attempted=0, rows_new=0,
                    )
                    for i in range(need)
                ]
                st.runs = injected + st.runs
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
    ap.add_argument("--assume-zero-yield", metavar="ID_OR_NAME",
                    help=f"红证：给该策略注入连续 {ZERO_YIELD_STREAK_LIMIT} 次零产出（只改内存）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="九条分支自证")
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
                        assume_missed_slot=args.assume_missed_slot, now=now,
                        assume_zero_yield=args.assume_zero_yield)
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
