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
#: 放弃（surface-abandoned-cells-on-run-row）：执行器这一轮**没有发出去的**请求数。
#: 与 `zero_yield` 的分界是「抓了没有」而不是「抓到没有」——`zero_yield` 意味着请求发出
#: 去了、上游一行都没给（去查上游）；`abandoned` 意味着这些格我们主动不抓了（去查那批
#: (concept, entity) 是不是在任何候选端点都没有数据）。两者同时存在时各自报各自的数，
#: 不许把一边并进另一边（合并后处置方向就只剩一个了）。
#:
#: **刻意不进 `NON_YIELD_OK`**：那一档的词是「合法不产出」，而放弃是**覆盖率损失**——
#: 放进同一档，读的人只会看到「合法不产出」。也**刻意不判红**：放弃是发放预算的设计
#: 行为，一个长尾里 40 个实体永远抓不到，逐轮判红就是一道常年红的闸（本仓结论：常年
#: 红的闸会被无视）。折中是每轮打一条**带数字的事实行**——存在感不降级，但不改 rc。
ABANDONED_STATUS = "abandoned"
#: 零产出按**连续次数**判，不按单次：单次「计划了却没抓到」在周末/长假/上游迟发时
#: 都会出现，逐次报警会得到一道每逢周末必红的闸（本仓已有结论：常年红的闸会被无视）。
#: 取 3 而不是 2：周末连着两天是常态形态，两天永不够；真断供到第三天就红。
ZERO_YIELD_STREAK_LIMIT = 3
NON_YIELD_OK = {"no_op", "redundant", "skipped"}
TERMINAL_STATUSES = {"success", *FAILED_STATUSES, *NON_YIELD_OK, "cancelled",
                     ZERO_YIELD_STATUS, ABANDONED_STATUS}
#: 认识的全部状态。**不认识的状态出现在最新一跳 ⇒ 判不出**，不许静默走进绿路。
#: 2026-11-15 量到 policy_runs 里另有 `aborted`（173 行，全在 08-30..09-18，只有 1 行
#: 属于启用策略且是 09-18 的老行），仓里没有一处定义它在 run 侧的语义（`expander` 里
#: 那个是 wave 状态）。猜一个含义去归类，等于替未定义的语义背书，所以只让它变成
#: 「判不出」，不变成绿也不变成红。
KNOWN_STATUSES = OPEN_STATUSES | TERMINAL_STATUSES

# ─── R5 慢下来（成功，但比自己的历史慢几倍）────────────────────────────────
# 为什么需要它：R0–R4 判的全是**状态**，没有一条判**耗时**。于是「运行成功、但每格
# 耗时涨了 3–8 倍」这个窗口完全没人看——2026-11-15 实测 policy 1（fund-pilot-series，
# 每天 06:37Z）：11-05..11-09 稳定 4.5s/计划格，11-11 起 13.5 → 6.8 → 18 → 15s，
# 11-15 撞上 3h 执行器上限被杀。中间那三天全是 `success`，**一条判据都没报**，
# 直到被杀才由 R1 说出来，而那时已经丢了三天数据。
# 判据是**自指的**（对这条策略自己最近若干轮的中位数），所以不吃「绝对耗时」这种
# 与被抓数据量强相关的口径；计划格数变了也不影响（`plan_cells` 在分母里）。
#: 至少要有这么多轮可用历史才判——否则「中位数」只是上一轮本身，噪声会被当趋势。
SLOW_MIN_HISTORY = 5
#: 最多看最近这么多轮（更早的形态与当前不是同一回事）。
SLOW_HISTORY_MAX = 10
#: 慢到历史中位数的几倍才开口。取 3：p1 那三天实测 2.2–4.0 倍，2 倍会被日常波动打中。
SLOW_FACTOR = 3.0
#: 还要**绝对**多花这么多秒/格，避免「4.5s 变 14s 只是噪声」这类小基数放大。
SLOW_MIN_DELTA_SECONDS = 2.0

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
    #: 有多少格**没去抓**（执行器的放弃预算用完了）。`None` 只表示「这一轮没报」，不表示
    #: 0——列是后加的（alembic 0025），旧执行器、以及迁移还没落地时，读到的都是 None。
    #: 判「供数中断」与「我们主动不抓」必须分开：前者去查上游，后者去查放弃预算。
    rows_abandoned: int | None = None


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
                       # | dangling_open_run | opaque_reason | slowed_down
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


def _abandoned_note(run: PolicyRun) -> str:
    """「这一轮放弃了多少格」（混合轮里连抓取行数一起念）；**未报**时返回空串。

    `None` 与 0 必须分开：把「没报」写成「放弃了 0 格」正是这一列存在的理由
    （见 `PolicyRun.rows_abandoned` 的注释）。同理 `0` 也不念——「放弃了 0 格」不是
    信息，读的人只需要在真放弃过时看到数字。

    混合轮（抓了一些、又放弃了一些）两个数并排念：spec 要求「两个数分开报」，只念
    放弃数会让「这一轮其实抓了 585 行」消失，而这两件事的结论完全不同。
    """
    n = run.rows_abandoned
    if not n:
        return ""
    bits = []
    if run.rows_attempted:
        bits.append(f"抓取 {run.rows_attempted} 行")
    bits.append(f"另有 {n} 格被放弃")
    return ("、".join(bits)
            + "（放弃＝执行器的预算用完：这些 (concept, entity) 在候选端点上都没有数据，"
              "请求**没有发出去**）")


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


def _per_cell_seconds(run: PolicyRun) -> float | None:
    """这一轮的「秒/计划格」。读不出（缺 `plan_cells`／没有终态时刻）返回 None。

    为什么用 `plan_cells` 而不是 `rows_attempted`：后者是**行**，一轮几行取决于抓到了
    多少数据、与被抓数据量强相关；而计划格数在同一策略上是稳定的，能当分母。
    2026-11-15 就是拿 rows 当分母，把 p1 的「变慢」读成了「量暴增」（差 40 倍的方向）。
    """
    if not run.plan_cells or run.plan_cells <= 0:
        return None
    started, finished = _as_utc(run.started_at), _as_utc(run.finished_at)
    if started is None or finished is None:
        return None
    seconds = (finished - started).total_seconds()
    if seconds <= 0:
        return None
    return seconds / run.plan_cells


def _slow_verdict(terminal: list[PolicyRun]) -> tuple[float, float, int] | None:
    """(本轮秒/格, 历史中位, 历史轮数)；不足以下结论返回 None。

    `terminal` 必须是**按 started_at 倒序**的终态行，第 0 条是本次判的对象。
    历史只取 `success`/`zero_yield` 这类「跑到底」的轮次：被杀掉的轮次（failed）
    本身耗时被上限截断，把它们混进基线会让基线虚高、进而掩盖真实的劣化。
    """
    current = _per_cell_seconds(terminal[0]) if terminal else None
    if current is None:
        return None
    history: list[float] = []
    for r in terminal[1:]:
        if r.status not in {"success", ZERO_YIELD_STATUS}:
            continue
        per_cell = _per_cell_seconds(r)
        if per_cell is None:
            continue
        history.append(per_cell)
        if len(history) >= SLOW_HISTORY_MAX:
            break
    if len(history) < SLOW_MIN_HISTORY:
        return None
    ordered = sorted(history)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2 else
              (ordered[mid - 1] + ordered[mid]) / 2.0)
    return current, median, len(history)


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
            note = _abandoned_note(latest)
            if streak >= ZERO_YIELD_STREAK_LIMIT:
                findings.append(Finding(
                    st.policy_id, st.name, "repeated_zero_yield",
                    f"最近 {streak} 次终态全是零产出（最近一次 "
                    f"{_as_utc(latest.started_at):%Y-%m-%d %H:%M}Z）：{shape}"
                    + (f"；{note}" if note else "")
                    + f"——连续 {ZERO_YIELD_STREAK_LIMIT} 次以上就不是「恰好没有新数据」，"
                    f"去看上游是否还在给数据（这条没有报错，job 每次都成功退出）",
                ))
            else:
                facts.append(f"{label}: 最近一次终态 zero_yield（{shape}）"
                             + (f"；{note}" if note else "")
                             + f"——未连续 {ZERO_YIELD_STREAK_LIMIT} 次，只报事实")
        elif latest.status == ABANDONED_STATUS:
            # 全放弃：`classify_yield` 只在 attempted==0 **且** rows_abandoned>0 时给这个
            # 状态，所以它绝不能被读成 zero_yield——「没抓到」与「没去抓」的处置相反。
            # 只报事实、不判红：放弃是预算的设计行为（长尾里总有一批实体三端点都没数据），
            # 逐轮判红会得到一道常年红的闸。但**必须带数字念出来**——它此前在账本里
            # 是 success 的形状，覆盖率掉了多少没人看得见。
            a = latest.rows_abandoned
            planned = latest.plan_cells
            facts.append(
                f"{label}: 最近一次终态 abandoned（计划 {planned if planned is not None else '?'} 格，"
                f"其中 {a} 格被放弃、其余格没有产出；抓取 0 次）"
                f"——这些 (concept, entity) 在候选端点上都没有数据，请求没有发出去；"
                f"不是供数中断（那档是 zero_yield），是覆盖率问题，不计命中"
            )
        elif latest.status in NON_YIELD_OK:
            # 混合轮走这里（attempted>0 ⇒ 状态是 redundant/success）：按 spec「两个数分开
            # 报」，放弃数必须跟着念，否则它被「合法不产出」这句话吞掉。
            note = _abandoned_note(latest)
            facts.append(f"{label}: 最近一次终态 {latest.status}"
                         f"（合法不产出，不计命中）" + (f"；{note}" if note else ""))
        else:
            note = _abandoned_note(latest)
            facts.append(f"{label}: 最近一次终态 {latest.status}"
                         + (f"；{note}" if note else ""))

        # R5 慢下来：**成功但比自己的历史慢**。只在最新一轮是 success 时判——
        # 失败那档已经由 R1 说出来了（同一处问题记两次会让命中数失真，同 R3 的选择）。
        # 它补的是 R0–R4 的结构性空白：那些判据全看**状态**，而劣化是先把耗时拉长、
        # 再撞上限才变成失败的；等 R1 开口时数据已经丢了好几天（2026-11-15 p1 实测）。
        if latest.status == "success":
            verdict = _slow_verdict(terminal)
            if verdict is not None:
                current, median, n = verdict
                if (median > 0 and current >= median * SLOW_FACTOR
                        and current - median >= SLOW_MIN_DELTA_SECONDS):
                    findings.append(Finding(
                        st.policy_id, st.name, "slowed_down",
                        f"这一轮每计划格 {current:.1f}s，而它自己最近 {n} 轮的中位数是 "
                        f"{median:.1f}s（{current / median:.1f} 倍）"
                        f"——运行**成功**、没有报错，所以状态类判据一条都不会响；"
                        f"按这个斜率会在撞上执行器上限（{executor_ceiling_seconds / 3600:.1f}h）"
                        f"时被杀，先看上游是不是在限流/变慢，别等它自己好",
                    ))


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
    # 局部导入：`--self-test` 是纯函数自证，本机不一定装了 sqlalchemy
    # （2026-11-16 实测：放到模块顶层直接 ModuleNotFoundError，连自证都跑不起来）。
    from sqlalchemy.exc import DBAPIError

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
            # `rows_abandoned` 是 2026-11-16 之后才有的列（alembic 0025）。闸是日频、跑在
            # 同一个库上，而迁移由 planner 镜像的 migrate_stage 建——两者谁先到不确定。
            # 读不到列**不能**让整道闸退 2（那会把「多了一列」变成全线失明），退回老列读，
            # 并把 runs 的 rows_abandoned 留成 None =「这一轮没报」，不是 0。
            base_sql = (
                "select policy_id, status, started_at, finished_at, coalesce(detail, ''), "
                "       plan_cells, rows_attempted, rows_new{extra} "
                "from (select policy_id, status, started_at, finished_at, detail, "
                "             plan_cells, rows_attempted, rows_new{extra}, "
                "             row_number() over (partition by policy_id "
                "                                order by started_at desc) rn "
                "        from policy_runs where policy_id = any(:ids)) t "
                "where rn <= 50 order by policy_id, started_at desc")
            has_abandoned = True
            try:
                runs = conn.execute(text(base_sql.format(extra=", rows_abandoned")),
                                    {"ids": ids}).all()
            except DBAPIError:
                # 会话已被这条失败语句污染，先回滚再用老列重跑。
                conn.rollback()
                runs = conn.execute(text(base_sql.format(extra="")), {"ids": ids}).all()
                has_abandoned = False
                print("ℹ policy_runs 里没有 rows_abandoned 列（alembic 0025 未落地）"
                      "——本轮放弃数一律按「未报」处理，不按 0", file=sys.stderr)
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
            rows_abandoned=(r[8] if has_abandoned else None),
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

    def run(status, hours_ago, detail="", *, cells=None, att=None, new=None, ab=None):
        return PolicyRun(status=status,
                         started_at=now - timedelta(hours=hours_ago), detail=detail,
                         plan_cells=cells, rows_attempted=att, rows_new=new,
                         rows_abandoned=ab)

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
        # 全放弃（surface-abandoned-cells-on-run-row）：45 格被放弃、0 次抓取。
        # 后面紧跟两次 zero_yield **故意凑不出连续 3 次**——放弃不能悄悄累进零产出那条
        # 连续计数（那是「上游断供」的告警，永远抓不到的长尾会把它变成常年红的闸）。
        PolicyState(10, "all-abandoned", "0 6 * * *", "UTC", [
            run("abandoned", 5, cells=630, att=0, new=0, ab=45),
            run("zero_yield", 29, cells=630, att=0, new=0),
            run("zero_yield", 53, cells=630, att=0, new=0),
        ]),
        # 混合轮：抓了 585 行、又放弃 45 格，一行新数据都没有 ⇒ 状态仍是 redundant。
        # 两个数必须并排念出来，否则「抓了 585 行」被「合法不产出」吞掉。
        PolicyState(11, "mixed", "0 6 * * *", "UTC", [
            run("redundant", 5, cells=630, att=585, new=0, ab=45),
        ]),
        # 连续计数的边界：**最新是 zero_yield**，中间夹一次 abandoned，再往前两次零产出。
        # 正确读数 = 连续 1 次（遇到 abandoned 就断）⇒ 不报；若把 abandoned 当零产出，
        # 就成了连续 4 次 ⇒ 误报 R1b。p10 那条臂钉不到这里：latest 是 abandoned 时走的是
        # 另一个分支，那段计数根本没执行（第一版就是这么写的，红证一跑就发现它不承重）。
        PolicyState(13, "streak-across-abandoned", "0 6 * * *", "UTC", [
            run("zero_yield", 5, cells=630, att=0, new=0),
            run("abandoned", 29, cells=630, att=0, new=0, ab=12),
            run("zero_yield", 53, cells=630, att=0, new=0),
            run("zero_yield", 77, cells=630, att=0, new=0),
        ]),
        # 未报（列缺失或旧执行器）：既不许念成「放弃了 0 格」，也不许念成「供数中断」
        # 之外的任何新东西——它就是老形态的 zero_yield。
        PolicyState(12, "not-reported", "0 6 * * *", "UTC", [
            run("zero_yield", 5, cells=630, att=0, new=0),
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

    # ── 放弃（surface-abandoned-cells-on-run-row）：三路夹具，每路断言**具体词与数字**。
    # 只断言「有事实行」会把「判据没跑、别的行顶上」读成绿。
    ab_facts = [f for f in facts if f.startswith("p10 all-abandoned")]
    # 断言的是**状态标签**（"终态 abandoned"），不是「这行里不出现 zero_yield 这个词」——
    # 那一版断言被自己的措辞打败了：这行刻意对比两者（"不是供数中断（那档是 zero_yield）"），
    # 于是「含 zero_yield」恰恰是设计。判据要钉在标签上，不能钉在整行字面上。
    ab_ok = (bool(ab_facts)
             and "终态 abandoned" in ab_facts[0]
             and "45" in ab_facts[0] and "630" in ab_facts[0])
    print(f"  {'✓' if ab_ok else '✗'} p10 全放弃念成 abandoned + 数字（45 格/计划 630）"
          f"：{(ab_facts[0][:90] + '…') if ab_facts else '没有事实行'}")
    # 放弃不许累进零产出的连续计数：p13 = 最新零产出 + 中间一次放弃 + 再往前两次零产出。
    # 正确读数是连续 1 次；把放弃当零产出就是连续 4 次 ⇒ 误报（而 p10 用 latest=abandoned
    # 钉不到这里——那条走的是另一个分支，计数根本没执行）。
    ab_streak_ok = not any(f.policy_id == 13 for f in findings)
    print(f"  {'✓' if ab_streak_ok else '✗'} p13 放弃不累进 zero_yield 连续计数"
          f"（最新零产出 +1 放弃 +2 零产出 ⇒ 连续 1 次，不报 R1b）")
    mixed = [f for f in facts if f.startswith("p11 mixed")]
    mixed_ok = (bool(mixed) and "585" in mixed[0] and "45" in mixed[0]
                and "被放弃" in mixed[0] and "redundant" in mixed[0])
    print(f"  {'✓' if mixed_ok else '✗'} p11 混合轮两个数并排念（抓 585 行 + 放弃 45 格）"
          f"：{(mixed[0][:90] + '…') if mixed else '没有事实行'}")
    nr = [f for f in facts if f.startswith("p12 not-reported")]
    nr_ok = (bool(nr) and "被放弃" not in nr[0] and "zero_yield" in nr[0]
             and "放弃" not in nr[0])
    print(f"  {'✓' if nr_ok else '✗'} p12 未报 ≠「放弃 0 格」：只按老形态说 zero_yield"
          f"：{(nr[0][:90] + '…') if nr else '没有事实行'}")
    ab_all_ok = ab_ok and ab_streak_ok and mixed_ok and nr_ok

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

    slow_ok = True

    def schk(name: str, expect: str, actual: str) -> None:
        nonlocal slow_ok
        hit = expect == actual
        print(f"  {'✓' if hit else '✗'} {name}"
              + ("" if hit else f"（期望 {expect!r}，实际 {actual!r}）"))
        slow_ok = slow_ok and hit

    # ── R5 慢下来：成对臂。只换「这一轮花了多久」与「历史够不够」，
    #    一个必须开口、一个必须闭嘴——少了下半路，「把 3 倍写成 1.5 倍」这种
    #    改错也能一路绿（数字全对、就是不判）。
    def run_t(status, hours_ago, minutes, *, cells):
        return PolicyRun(status=status,
                         started_at=now - timedelta(hours=hours_ago),
                         finished_at=now - timedelta(hours=hours_ago) + timedelta(minutes=minutes),
                         plan_cells=cells)

    def _slow_state(pid, latest_minutes, *, cells=300, history=(15, 15, 15, 15, 15),
                    hist_cells=200, latest_status="success"):
        runs = [run_t(latest_status, 5, latest_minutes, cells=cells)]
        for k, m in enumerate(history, start=1):
            runs.append(run_t("success", 5 + 24 * k, m, cells=hist_cells))
        return PolicyState(pid, f"slow-{pid}", "0 6 * * *", "UTC", runs)

    def _kinds(st):
        return {f.rule for f in judge([st], now=now)[0]}

    slow = judge([_slow_state(21, 195)], now=now)[0]      # 39.0s/格 vs 历史 4.5s/格
    slow_msg = slow[0].detail if slow else ""
    schk("R5 成功但慢 8.7 倍 ⇒ 命中 slowed_down", "yes",
        "yes" if [f.rule for f in slow] == ["slowed_down"] else f"命中={[f.rule for f in slow]}")
    schk("R5 报文念出两侧的秒/格与历史轮数", "yes",
        "yes" if ("39.0s" in slow_msg and "4.5s" in slow_msg and "最近 5 轮" in slow_msg)
        else f"报文={slow_msg[:110]!r}")
    # 成对臂取 **2.7 倍**（12.0s vs 4.5s）而不是「更快」那一档：更快那一档在阈值
    # 改成 1.5 时也不会红，等于没验到阈值本身（第一版就是这么漏的）。
    schk("R5 慢 2.7 倍 ⇒ 不命中（阈值在 3 倍，成对臂）", "no",
        "yes" if _kinds(_slow_state(22, 60)) else "no")
    schk("R5 历史只有 4 轮 ⇒ 不判（中位数还不成形）", "no",
        "yes" if _kinds(_slow_state(23, 195, history=(15, 15, 15, 15))) else "no")
    schk("R5 缺 plan_cells ⇒ 不判（判不出不是正常）", "no",
        "yes" if {f.rule for f in judge([PolicyState(24, "slow-24", "0 6 * * *", "UTC", [
            PolicyRun(status="success", started_at=now - timedelta(hours=5),
                      finished_at=now - timedelta(hours=2))])], now=now)[0]} else "no")
    schk("R5 最新一轮是 failed ⇒ 不重复记（R1 已经说过）", "no",
        "yes" if "slowed_down" in _kinds(_slow_state(25, 195, latest_status="failed")) else "no")
    # 被杀的轮次不许进基线：它们的历史时长本身被上限截断，混进去会把基线抬高、
    # 反而掩盖真实劣化。这里 3 个正常历史 + 2 个 failed 历史 ⇒ 可用历史只有 3 条 ⇒ 不判。
    two_failed = PolicyState(26, "slow-26", "0 6 * * *", "UTC", [
        run_t("success", 5, 195, cells=300),
        run_t("success", 29, 15, cells=200), run_t("success", 53, 15, cells=200),
        run_t("success", 77, 15, cells=200),
        run_t("failed", 101, 400, cells=200), run_t("failed", 125, 400, cells=200),
    ])
    schk("R5 被杀轮次不进基线（3 条可用历史 ⇒ 不判）", "no",
        "yes" if _kinds(two_failed) else "no")

    return 0 if (ok and green and noop_fact and zy_fact and split_ok and und_ok
                 and ab_all_ok and tz_ok and bad_ok and slow_ok) else 1


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
