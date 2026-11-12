"""Failure + stale-run scan entrypoint (add-crawl-visibility).

``python -m fd_open_data_mcp.visibility.scan``

Runs as its own k3s CronJob (``10,25,40,55 * * * *`` — 3 min after the
reconciler ticks at ``:07/:22/:37/:52``, so it reads freshest post-tick state).
Reads ``policy_runs`` past the scan watermark, detects newly-failed / newly-
refused / newly-stale runs, dedups each via the Redis alerted-set (each
(run, event) alerted once, 7-day TTL), batches ALL new events in the window
into ONE message, sends it, advances the watermark, and prints a JSON summary.

It also checks worker **credential expiry** (rotate-worker-cluster-credentials
3.2), when ``/kubeconfigs`` is mounted. That check rides this tick instead of
owning a CronJob, and its dedup window is 24h rather than 7 days: a failed run
is a one-off event, but "this credential is drifting toward expiry" stays true
and must re-nag.

Read-only: it never mutates ``policy_runs`` (a stale run is flagged, not
closed). A sink failure or missing token never breaks a tick — the watermark
still advances, and a missing credential mount just skips that one check.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys

from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import Cluster, CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility import credentials, snapshot
from fd_open_data_mcp.visibility.notifiers.factory import get_notifier
from fd_open_data_mcp.visibility import state

logger = logging.getLogger(__name__)

_STALE_MIN = int(os.environ.get("SCRAW_STALE_MINUTES", "90"))

# 凭据到期这一类事件的复查间隔。别的告警是"一次性事件"（一次失败只该报一次），
# 而"凭据正在滑向过期"是一个**持续为真的条件**：7 天静默对一个 10 天跑道太长，
# 所以每 24h 重提一次，直到刷新把它消掉。
_CREDENTIAL_REMIND_SEC = 24 * 3600

# 凭据挂载点。没挂就静默跳过——"没挂载"不等于"有风险"，而这条检查是附赠的，
# 不能因为它让本来的失败/停滞扫描整轮炸掉。
_CREDENTIALS_DIR = os.environ.get("SCRAW_KUBECONFIGS_DIR",
                                  credentials.DEFAULT_KUBECONFIGS_DIR)


def _classify(run: PolicyRun) -> str | None:
    """Map a terminal run to an alert event class, or None (not alertable).

    fix-silent-zero-yield-crawls: ``zero_yield`` joins ``failed`` in the
    terminal non-success set — it IS the outage signal this watcher was blind
    to. ``no_op`` (empty plan) and ``redundant`` (fetched, nothing new) are
    recorded but non-alerting; the digest surfaces redundant STREAKS instead.
    """
    if run.status == "failed":
        if (run.detail or "").lower().startswith("refused:"):
            return "refused"
        return "failed"
    if run.status == "zero_yield":
        return "zero_yield"
    return None


# Terminal non-success statuses the scan queries for (spec crawl-visibility:
# failed, zero_yield, including guardrail refusals recorded as failed rows).
#
# 2026-11-12：升为**公开**常量并被面板 KPI 复用。此前面板自己算成功率，把
# ``no_op``（空计划，本函数明确判为"不可告警"）也算进了失败分母——线上 24h
# 实测 failed 3 / no_op 3 / success 5，面板报 45.5%，而真正可判定的失败率是
# 3/8。同一个"什么算失败"必须只有一处定义，否则两处各自漂移。
ALERT_STATUSES = ("failed", "zero_yield")

#: 记录但不告警、也不该计入成功率的终态（"这一轮没有信号"）。
NO_SIGNAL_STATUSES = ("no_op", "redundant")

#: 兼容旧名（仓内/仓外仍可能有引用）。
_ALERT_STATUSES = ALERT_STATUSES


def check_credential_expiry(now: dt.datetime | None = None) -> tuple[list[dict], bool]:
    """worker 派发凭据的到期检查。返回 (新告警项, 是否真的查了)。

    为什么挂在 scan 里而不是独立 CronJob：这条检查属于"系统还健康吗"，
    而 scan 就是那条每 15 分钟问一次这个问题、且**自己带推送通道和去重**的作业。
    再起一条 CronJob 等于多一个没人看的东西——它自己失败时同样悄无声息，
    于是"到期告警"本身也到期了，问题只是往旁边挪了一格。

    判定复用 `credentials.lifetime_problems`（与巡检脚本同一份实现）。

    三个刻意选择：
    - 目录不存在 → 返回 ``checked=False``，不告警。没挂载是部署形态差异，
      不是风险；把它当告警会让每个未挂载的部署永久假红。
    - 读 token 失败 → 归为 unknown，`lifetime_problems` 不管它（不知道 ≠ 违规），
      由巡检脚本去判，因为只有它知道哪些集群"该有"。
    - legacy（无 exp）→ 不告警。它是选择不是故障（见 DECISION.md）。

    第四个不是选择，是坑：目录在、但里面**一个 `.token` 都没有**（密钥键改名成
    `cheap.crt`、Secret 被换空、路径挂错一级）时，`read_lifetimes` 返回 `{}`，
    `lifetime_problems` 也只能返回 `[]`——于是 ``checked=True`` + `credential=[]`，
    与"所有凭据都健康"**完全同形**。2026-11-12 实测线上正是这一形（当时确实健康，
    但两种情形不可区分）。`credentials_checked` 的语义因此收紧为"**至少评过一个
    token**"：没评到就不算查过。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        lifetimes = credentials.read_lifetimes(_CREDENTIALS_DIR)
    except FileNotFoundError:
        logger.info("credential expiry: %s 未挂载，跳过", _CREDENTIALS_DIR)
        return [], False
    except OSError as exc:
        # 挂载在但读不动：这确实是异常，但也别把整轮 scan 拖垮。
        logger.warning("credential expiry: 读 %s 失败（%s），本轮跳过",
                       _CREDENTIALS_DIR, exc)
        return [], False

    if not lifetimes:
        # 目录在、零个 `<cluster>.token`：不是"都健康"，是"这条检查没生效"。
        logger.warning("credential expiry: %s 里没有任何 <cluster>.token——"
                       "挂载或 Secret 键名不对，到期告警此刻是哑的", _CREDENTIALS_DIR)
        return [], False

    problems = credentials.lifetime_problems(lifetimes)
    if not problems:
        return [], True

    # 逐集群去重：一个集群一条，24h 内不重复。问题文案直接用 credentials 的
    # 那一份（`lifetime_problem_for`），这里不再判断一次阈值。
    fresh: list[dict] = []
    for name in sorted(lifetimes):
        life = lifetimes[name]
        problem = credentials.lifetime_problem_for(name, life)
        if problem is None:
            continue
        subject = f"credential:{name}"
        if state.already_alerted(subject, "credential"):
            continue
        rem = life["remaining_s"]
        fresh.append({
            "cluster": name,
            "expired": rem is not None and rem < 0,
            "remaining": credentials.fmt_seconds(rem),
            "lifetime": credentials.fmt_seconds(life["lifetime_s"]),
            "expires_at": (dt.datetime.fromtimestamp(life["expires_at"], dt.timezone.utc)
                           .isoformat() if life["expires_at"] else None),
            "problem": problem,
            "_subject": subject,
        })
    return fresh, True


def check_credential_surface(session: Session) -> tuple[list[dict], bool]:
    """挂载面与 `clusters` 表的一致性。返回 (新告警项, 是否真的查了)。

    为什么和到期检查一样挂在 scan 里：这类问题**没有运行期症状**。派发只按
    `enabled=true` 选集群，所以"禁用集群上留着一把可用凭据"永远不会被用到，也就
    永远不会报错；而"启用的集群缺凭据"要等到真正派发时才 401。2026-11-12 实测：
    前者在线上存在了几个小时，是我排查另一件事时撞见的，不是被告知的。

    三个刻意选择（与到期检查同一姿态）：
    - 目录不存在 → ``checked=False``，静默跳过。没挂载是部署形态差异，不是风险。
    - `clusters` 表读不到 → 跳过并记日志，不把整轮 scan 拖垮。
    - **只报告，不处置**：自动删一把可能正被别处使用的凭据，比留个孤儿更难收场。
    """
    try:
        mounted = credentials.mounted_clusters(_CREDENTIALS_DIR)
    except FileNotFoundError:
        logger.info("credential surface: %s 未挂载，跳过", _CREDENTIALS_DIR)
        return [], False
    except OSError as exc:
        logger.warning("credential surface: 读 %s 失败（%s），本轮跳过",
                       _CREDENTIALS_DIR, exc)
        return [], False

    try:
        rows = session.query(Cluster.name, Cluster.enabled).all()
    except Exception as exc:  # 库抖动不该让整轮扫描失败
        logger.warning("credential surface: 读 clusters 表失败（%s），本轮跳过", exc)
        return [], False

    clusters = {str(name): bool(enabled) for name, enabled in rows}
    fresh: list[dict] = []
    for problem in credentials.surface_problems(clusters, mounted):
        subject = f"credential-surface:{problem['cluster']}:{problem['kind']}"
        if state.already_alerted(subject, "credential-surface"):
            continue
        fresh.append({**problem, "_subject": subject})
    return fresh, True


def scan_once(session: Session, now: dt.datetime | None = None) -> dict:
    """One scan tick. Returns a summary dict (also printed by ``main``)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    notifier = get_notifier()

    summary = {"scanned_at": snapshot._iso(now), "failed": [], "refused": [],
               "zero_yield": [], "stale": [], "credential": [],
               "credentials_checked": False, "notified": False}

    # 1. terminal runs since the watermark (failed / refused / zero_yield)
    wm = state.get_scan_watermark()
    q = (session.query(PolicyRun, CrawlPolicy.name)
         .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
         .filter(PolicyRun.status.in_(_ALERT_STATUSES)))
    if wm is not None:
        # The DB column is TIMESTAMP WITHOUT TIME ZONE (naive-UTC writer
        # contract), so filter against a naive cutoff to avoid a naive/aware
        # mismatch on Postgres. The watermark itself is stored as a UTC epoch.
        wm_naive = dt.datetime.fromtimestamp(wm, dt.timezone.utc).replace(tzinfo=None)
        q = q.filter(func.coalesce(PolicyRun.finished_at, PolicyRun.started_at) > wm_naive)
    terminal = q.order_by(PolicyRun.started_at.asc()).all()

    # 2. stale runs (running too long) — independent of the watermark, deduped
    stale = snapshot.stale_runs(session, stale_min=_STALE_MIN)

    new_failed: list[dict] = []
    new_refused: list[dict] = []
    new_zero_yield: list[dict] = []
    for run, pname in terminal:
        event = _classify(run)
        if event is None:
            continue
        if state.already_alerted(run.id, event):
            continue
        rec = {
            "run_id": run.id, "policy": pname, "policy_id": run.policy_id,
            "event": event, "status": run.status,
            "datasources": snapshot._plan_datasources(run.plan_json),
            "job_ref": run.job_ref, "cluster_id": run.cluster_id,
            "started_at": snapshot._iso(run.started_at),
            "finished_at": snapshot._iso(run.finished_at),
            "detail": (run.detail or "")[:160],
        }
        if event == "zero_yield":
            # plan_cells travels WITH the alert so the operator can tell a
            # broken fetch path from an empty plan without querying the DB
            rec["plan_cells"] = run.plan_cells
            rec["rows_attempted"] = run.rows_attempted
            new_zero_yield.append(rec)
        elif event == "refused":
            new_refused.append(rec)
        else:
            new_failed.append(rec)

    new_stale: list[dict] = []
    for r in stale:
        if state.already_alerted(r["id"], "stale"):
            continue
        new_stale.append({
            "run_id": r["id"], "policy": r["policy"], "policy_id": r["policy_id"],
            "event": "stale", "age_minutes": r["age_minutes"],
            "datasources": r["datasources"], "job_ref": r["job_ref"],
            "cluster_id": r["cluster_id"], "started_at": r["started_at"],
        })

    summary["failed"] = new_failed
    summary["refused"] = new_refused
    summary["zero_yield"] = new_zero_yield
    summary["stale"] = new_stale

    # 2b. worker 派发凭据到期（rotate-worker-cluster-credentials 3.2）。
    # 放在运行态扫描之后、合并推送之前：这样它和失败/停滞共用一条消息，
    # 不额外消耗 ServerChan 的推送额度。
    new_credential, checked = check_credential_expiry(now)
    summary["credential"] = new_credential
    summary["credentials_checked"] = checked

    # 2c. 挂载面 vs clusters 表（alert-on-credential-surface-mismatch）。
    # 与 2b 同一个理由：折进同一条消息、共用通道与去重。判据在包里
    # （`credentials.surface_problems`），离线巡检脚本调的是同一份。
    new_surface, surface_checked = check_credential_surface(session)
    summary["credential_surface"] = new_surface
    summary["credential_surface_checked"] = surface_checked

    # 3. batch all new events into ONE message (ServerChan rate-limit aware)
    n_total = (len(new_failed) + len(new_refused) + len(new_zero_yield)
               + len(new_stale) + len(new_credential) + len(new_surface))
    if n_total > 0:
        title, body = _format_message(new_failed, new_refused, new_zero_yield,
                                      new_stale, new_credential, new_surface)
        notifier.send(title, body, level="error")
        summary["notified"] = True
        # mark only AFTER a send attempt so a sink outage doesn't drop alerts
        for rec in new_failed + new_refused + new_zero_yield:
            state.mark_alerted(rec["run_id"], rec["event"])
        for rec in new_stale:
            state.mark_alerted(rec["run_id"], "stale")
        for rec in new_credential:
            state.mark_alerted(rec["_subject"], "credential",
                               ttl_sec=_CREDENTIAL_REMIND_SEC)
        for rec in new_surface:
            state.mark_alerted(rec["_subject"], "credential-surface",
                               ttl_sec=_CREDENTIAL_REMIND_SEC)

    # 4. advance the watermark to the newest terminal run covered
    if terminal:
        newest = max((r.finished_at or r.started_at) for r, _ in terminal)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=dt.timezone.utc)
        state.set_scan_watermark(newest.timestamp())

    return summary


def _format_message(failed: list[dict], refused: list[dict],
                    zero_yield: list[dict], stale: list[dict],
                    credential: list[dict] | None = None,
                    surface: list[dict] | None = None) -> tuple[str, str]:
    credential = credential or []
    surface = surface or []
    n = (len(failed) + len(refused) + len(zero_yield) + len(stale)
         + len(credential) + len(surface))
    alerting = failed or refused or zero_yield
    if alerting:
        title = f"🚨 CRAWL FAILURE · {n} run(s)"
    elif credential:
        # 凭据到期不是"运行失败"，但同样是"今天不处理明天就断"的事——给它自己的标题，
        # 免得操作者在一串失败里漏掉它。
        # 标题在下面被截到 32 字（ServerChan 的限制），而
        # `🔑 WORKER CREDENTIAL · 1 cluster(s)` 是 38 字——实测被截成
        # `🔑 WORKER CREDENTIAL · 1 cluster(`，**数量后面的单位被吃掉**，
        # 只剩一个开括号。所以这里不写单位，把集群数留在可见范围内。
        title = f"🔑 WORKER CREDENTIAL · {len(credential)}"
    elif surface:
        # 面不一致（孤儿/禁用却有/启用却缺）与到期是两件事：一个要"去刷新"，
        # 一个要"去改配置"。混用一个标题会让人按错处置。
        title = f"🔑 CREDENTIAL SURFACE · {len(surface)}"
    else:
        title = f"⏳ CRAWL STALE · {n} run(s)"
    lines: list[str] = []
    for rec in failed:
        ds = " → ".join(rec["datasources"]) or "?"
        lines.append(f" • {rec['policy']}   {ds}   {rec['detail'].splitlines()[0] if rec['detail'] else ''}")
    for rec in refused:
        ds = " → ".join(rec["datasources"]) or "?"
        lines.append(f" • {rec['policy']}   REFUSED   {rec['detail'].splitlines()[0] if rec['detail'] else ''}")
    for rec in zero_yield:
        ds = " → ".join(rec["datasources"]) or "?"
        # plan_cells separates "planned work, fetched nothing" (the outage) from
        # an empty plan; rows_attempted distinguishes a dead fetch path
        lines.append(f" • {rec['policy']}   ZERO-YIELD ({rec.get('plan_cells')} cells, "
                     f"{rec.get('rows_attempted')} rows attempted)   {ds}")
    for rec in stale:
        ds = " → ".join(rec["datasources"]) or "?"
        lines.append(f" • {rec['policy']}   stale {rec['age_minutes']}min   {rec['job_ref'] or ''}")
    for rec in credential:
        lines.append(f" • {rec['problem']}")
    for rec in surface:
        lines.append(f" • {rec['problem']}")
    body = "\n".join(lines)
    if alerting:
        jobs = ", ".join(r["job_ref"] for r in (failed + refused + zero_yield) if r.get("job_ref"))
        body += f"\njob: {jobs}" if jobs else ""
        body += '\n→ ask Claude: "scraw status"'
    if credential:
        body += ('\n→ 刷新：python3 scripts/rotate_worker_credential.py --cluster '
                 f'{credential[0]["cluster"]} --mode bound --apply'
                 if len(credential) == 1 else
                 '\n→ 刷新：python3 scripts/rotate_worker_credential.py --cluster <name> '
                 '--mode bound --apply（或等 CI 的 refresh-worker-credentials）')
    if surface:
        # 处置与"到期"不同：孤儿/禁用是**删挂载**，缺失是**签发**。给错命令等于教人
        # 用错办法，所以按 kind 分开写，而不是复用上面那条刷新提示。
        missing = [r["cluster"] for r in surface if r["kind"] == "missing"]
        stale_keys = [r["cluster"] for r in surface if r["kind"] != "missing"]
        if stale_keys:
            body += (f"\n→ 清理：删掉挂载里的 {'/'.join(f'{c}.token' for c in stale_keys)}"
                     "（disabled 或表外集群不该留凭据）")
        if missing:
            body += ('\n→ 签发：python3 scripts/rotate_worker_credential.py --cluster '
                     f'{missing[0]} --mode bound --apply' if len(missing) == 1 else
                     '\n→ 签发：python3 scripts/rotate_worker_credential.py --cluster <name> '
                     '--mode bound --apply（缺凭据的集群逐个签）')
    return title[:32], body


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = get_database().get_session()
    try:
        summary = scan_once(session)
    finally:
        session.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # non-zero exit only if something genuinely broke (not "no events found")
    if summary.get("notified") is None and (summary["failed"] or summary["stale"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
