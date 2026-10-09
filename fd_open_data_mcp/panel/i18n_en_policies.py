"""Policies group EN entries (panel-rbac-i18n-refresh 4.3).

Group dictionary for the policy templates (policies / policy_edit /
estimate / _policy_row). Keys are the templates' verbatim zh copy; values
follow the English that used to ride along in ``<span class="en">`` /
inline mixed runs. ``i18n.py`` merges this module's ``EN_ENTRIES`` into
``EN`` at import time (zh locale never consults it).

Notes on shared keys:
- "第" matches the indicators group's paging value ("Page"); "条 policies"
  / "页" split the count envelope the same way ("5 条 policies · 第 1/1
  页" stays byte-identical in zh; en reads "5 policies · Page 1/1", "页"
  has no en remainder).
- "实体" / "实体类型" override the indicators group's detail-page calibers
  ("entity" / "Entity") with this board's original header/label calibers.
- "频率" / "概念" / "开始" / "结束" / "保留" deliberately NOT redefined:
  indicators/sources/shell own those calibers ("Freq" / "Concept" /
  "Started" / "Finished" / "Keep") and sort after this file, so a same-key
  entry here would be dead weight.
"""
from __future__ import annotations

EN_ENTRIES: dict[str, str] = {
    # ── policies.html (list) ───────────────────────────────────────────────
    "从频率模板新建": "New from frequency template",
    "搜索 名称/实体/id": "search name/entity/id",
    "搜索策略": "Search policies",
    "清除": "Clear",
    "条 policies": "policies",
    "第": "Page",  # same value as the indicators group's shared paging entry
    "页": "",  # en remainder folded into "第" → "Page 1/1"
    "共": "Total",
    "实体": "Entity",  # overrides indicators' "entity" — header caliber
    "上次运行": "Last run",
    "没有匹配的策略": "No policies match",
    "创建一个": "Create one",

    # ── policy_edit.html (editor, also the frequency template) ────────────
    "模板策略": "Template policy",
    "编辑策略": "Edit policy",
    "新建策略": "New policy",
    "已预选通过校验的": "Pre-selected the verified",
    "概念并给出匹配的调度": "concepts and proposed the matching schedule below",
    "保存前可任意调整；结果是一条普通策略":
        "adjust anything before saving; the result is an ordinary policy",
    "标识": "Identity",
    "实体类型": "Entity type",  # overrides indicators' "Entity" — keeps the nuance
    "实体 ID": "Entity IDs",
    "（逗号分隔；留空 = 该类型全部）": "(comma-separated; empty = all)",
    "（逗号分隔；空 = 全部）": "(comma; empty = all)",
    "抓取范围": "Crawl scope",
    "源过滤": "Source filter",
    "日期策略": "Date policy",
    "回溯天数": "Trailing days",
    "Cron 表达式": "Cron expr",
    "时区": "Timezone",
    "强制（绕过抓取上限）": "force (override fetch cap)",
    "运行一次（不建策略）": "Run once (no policy)",
    "不保存策略，直接运行此计划一次":
        "Run this plan once without saving a policy — same fetch ceiling applies",
    "预估": "Estimate",

    # ── estimate.html (htmx partial) ───────────────────────────────────────
    "抓取": "fetches",
    "个概念": "concepts",
    "模式": "Mode",
    "上限": "cap",
    "无法路由": "unroutable",
    "未映射实体": "unmapped entities",

    # ── _policy_row.html (inline-action row) ───────────────────────────────
    "立即运行": "Run now",
    "绕过 cron，但不绕过护栏": "bypasses cron, not guardrails",
    "确认删除": "confirm",
}
