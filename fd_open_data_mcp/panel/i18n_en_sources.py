"""Sources/funnel/data group EN entries (panel-rbac-i18n-refresh 4.3).

Group dictionary for the platform-source templates (sources /
source_detail / partial_sources_results / _source_row), the discovery
funnel (funnel / partial_funnel) and the data-coverage board (data).
Keys are the templates' verbatim zh copy — for inline 中英混排 runs (badges,
tooltips, chips, zh-line + en-line gloss pairs) the key keeps the whole
mixed string so the zh render stays byte-identical, and the value is the
English that used to ride along; ``.en``-span headings collapse to the bare
zh key. ``i18n.py`` merges this module's ``EN_ENTRIES`` into ``EN`` at
import time (zh locale never consults it).

Notes on shared keys:
- "源" overrides the core nav value ("Sources") with the table-header
  caliber "Source" (same override as the indicators group).
- "启用 Enabled" / "创建 Created" stay compound keys instead of overriding
  the bare words: bare 启用 is the policies toggle verb ("Enable") and bare
  创建 the indicators "Create" button — the headers need the past-participle
  caliber without disturbing them.
- Page <title> blocks keep their bilingual form via whole-string keys
  (tests assert the English half on zh pages, e.g. "Discovery funnel").
"""
from __future__ import annotations

EN_ENTRIES: dict[str, str] = {
    # ── page titles / shells ────────────────────────────────────────────────
    "平台源 Platform sources · 爬虫控制台": "Platform sources · Crawl Control Center",
    "平台源": "Platform sources",
    "候选源漏斗 Discovery funnel · 爬虫控制台": "Discovery funnel · Crawl Control Center",
    "候选源漏斗": "Discovery funnel",
    "数据覆盖 Data coverage · 爬虫控制台": "Data coverage · Crawl Control Center",
    "数据覆盖": "Data coverage",
    "爬虫控制台": "Crawl Control Center",
    "源": "Source",  # overrides core "Sources" — table-header caliber

    # ── sources.html (inventory intro) ─────────────────────────────────────
    "清单来自内容仓 manifest 与联邦成员登记（crawl_sources，dispatcher 每 5 分钟镜像）。"
    "Inventory mirrored from content-repo manifests + federation members.":
        "Inventory mirrored from content-repo manifests + federation members.",
    "schedule 为空 = 未点亮 not lit.": "An empty schedule = not lit.",

    # ── partial_sources_results.html (chips / filters / headers) ────────────
    "全部 All": "All",
    "已点亮 lit": "lit",
    "未点亮 not lit": "not lit",
    "停滞 stalled": "stalled",
    "站点 site": "site",
    "全部站点 all sites": "all sites",
    "筛选 Filter": "Filter",
    "站点": "Site",
    "调度": "Schedule",
    # compound key: bare 启用 stays the core button verb "Enable" used by
    # the policies toggles
    "启用 Enabled": "Enabled",
    "最近运行": "Last run",
    "产出行": "Rows",
    "待运行": "Pending",
    "没有匹配的源 No matching sources.": "No matching sources.",

    # ── _source_row.html (inventory row) ────────────────────────────────────
    "联邦成员 federated member — runner 声明触发 triggers by runner declaration":
        "federated member — triggers by runner declaration",
    "联邦 fed": "fed",
    "冻结 frozen": "frozen",
    "❄ 冻结 frozen": "❄ frozen",
    "已点亮但超过 7 天无运行记录 lit but no run in >7 days":
        "lit but no run in >7 days",
    "⚠ 停滞 stalled": "⚠ stalled",
    "启用 ON": "ON",
    "停用 OFF": "OFF",
    "从未运行 never ran": "never ran",
    "待运行 pending": "pending",
    "写入 pending_runs，由站点 dispatcher 认领执行 queues a pending_runs row for the site dispatcher":
        "queues a pending_runs row for the site dispatcher",
    "立即触发 trigger": "trigger",

    # ── source_detail.html (banners / metadata / pending / runs) ────────────
    "停滞提示 Stalled": "Stalled",
    "该源已点亮（schedule=": "lit (schedule=",
    "）但超过 7 天没有任何 crawl_runs 记录 lit but no run recorded in >7 days":
        ") but no crawl_runs record in >7 days",
    "停滞是提示，不是错误 a hint, not an error.": "a hint, not an error.",
    "冻结成员 Frozen member": "Frozen member",
    "触发被拒绝，登记保留可见 triggers are refused; the registration stays visible.":
        "triggers are refused; the registration stays visible.",
    "类别": "Kind",
    "最近提交": "Last commit",
    "清单更新": "Manifest updated",
    "联邦成员 federated member — 按声明触发 triggers by runner declaration":
        "federated member — triggers by runner declaration",
    "联邦 federated": "federated",
    "平台 platform": "platform",
    "未点亮 not lit（无自动运行 no automatic runs）": "not lit (no automatic runs)",
    "runner 声明 runner declaration": "runner declaration",
    "超时 timeout": "timeout",
    "未声明 no declaration": "no declaration",
    "（冻结成员 frozen member）": "(frozen member)",
    "认证源 authenticated": "authenticated",
    "登录身份见 the identity pool lives on the": "the identity pool lives on the",
    "认证面板 auth panel": "auth panel",
    "来自发现流水线 from discovery pipeline": "from discovery pipeline",
    "批准于 approved": "approved",
    "模型 model": "model",
    "查看漏斗 view funnel": "view funnel",
    "立即触发 trigger now": "trigger now",
    "写入 pending_runs（requested_by=panel），由站点 dispatcher 认领执行 "
    "queues a pending_runs row for the site dispatcher.":
        "queues a pending_runs row for the site dispatcher.",
    "请求者": "Requested by",
    # compound key: bare 创建 stays the indicators group's "Create" button
    "创建 Created": "Created",
    "没有待运行务 No pending runs.": "No pending runs.",
    "最近 20 次运行": "Last 20 runs",
    "开始": "Started",
    "结束": "Finished",
    "质量": "Quality",
    "触发行": "Trigger",
    "错误": "Error",
    "尚无运行记录 No runs recorded yet.": "No runs recorded yet.",

    # ── funnel.html (page shell) ────────────────────────────────────────────
    "发现 → 候选 → 分析 → 生成 manifest 的各阶段计数与最新待批准清单，来自中央流水线表。"
    "Stage counts and the latest approval queue over the central pipeline tables.":
        "Stage counts and the latest approval queue over the central pipeline tables.",
    "审批在 harness 工具面完成（agent 操作），此处只读。"
    "Approval happens on the harness tool surface (agent-operated); this view is read-only.":
        "Approval happens on the harness tool surface (agent-operated); "
        "this view is read-only.",
    "加载中 loading…": "loading…",

    # ── partial_funnel.html (stage counts / queues) ─────────────────────────
    "发现": "Discoveries",
    "候选": "Candidates",
    "已分析": "Analysed",
    "已生成": "Generated",
    "待批准": "Pending",
    "已批准": "Approved",
    "已落地": "Landed",
    "最新待批准": "Latest pending approval",
    "源名": "Source name",
    "模型": "Model",
    "更新": "Updated",
    "队列为空，没有待批准 manifest Empty queue — no draft manifests.":
        "Empty queue — no draft manifests.",
    "「已落地」= 源名精确匹配 crawl_sources（查询期判定，无外键）。"
    "\"Landed\" = source_name exactly matches a crawl_sources row "
    "(query-time fact, no FK).":
        "\"Landed\" = source_name exactly matches a crawl_sources row "
        "(query-time fact, no FK).",
    "已落地 landed": "landed",
    "已批准 approved": "approved",
    "尚无已批准 manifest No approved manifests yet.": "No approved manifests yet.",

    # ── data.html (coverage / census / heatmap / filters) ───────────────────
    "个概念有观测 concepts with observations": "concepts with observations",
    "local rows 本地行": "local rows",
    "覆盖为实时 live coverage": "live coverage",
    "覆盖采样于 coverage sampled": "coverage sampled",
    "前": "ago",
    "陈旧 stale": "stale",
    "across all stores 全部存储合计": "across all stores",
    "census 尚未采样 not yet sampled": "not yet sampled",
    "刷新覆盖 Refresh coverage": "Refresh coverage",
    "存储": "Stores",
    "（最新普查 latest census）": "(latest census)",
    "刷新普查 Refresh census": "Refresh census",
    "行数": "Rows",
    "大小": "Size",
    "分块": "Chunks",
    "数据截至": "Data through",
    "采样于": "Sampled",
    "错误 error": "error",
    "前 ago": "ago",
    "概念新鲜度": "Concept freshness",
    "概念新鲜度分布 concept freshness by bucket": "concept freshness by bucket",
    "清除新鲜度筛选 Clear freshness": "Clear freshness",
    "概念 id concept id": "concept id",
    "实体类型 entity type (stock/fund/country…)": "entity type (stock/fund/country…)",
    "实体类型 entity type": "entity type",
    "清除 Clear": "Clear",
    "概念": "Concept",
    "分类": "Category",
    "最新日期": "Latest date",
    "数据源": "Sources",
    "上次抓取": "Last fetch",
    "没有匹配的观测 No observations match.": "No observations match.",
}
