"""Indicators observatory board EN entries (panel-rbac-i18n-refresh 4.3).

Group dictionary for the indicator observatory templates (indicators /
families / family / detail / relations / coverage / graph / scopes /
scope_detail + the two partials). Keys are the templates' verbatim zh
copy; values follow the English that used to ride along in ``<span
class="en">`` / inline mixed runs. ``i18n.py`` merges this module's
``EN_ENTRIES`` into ``EN`` at import time (zh locale never consults it).

Notes on shared keys:
- "源" overrides the core nav value ("Sources") with the table-header
  caliber "Source" used by this board's th/aria labels.
- "条 entries" / "第" / "页 page" split the paging envelope line so the
  zh render stays byte-identical ("12 条 entries · 第 1/1 页 page");
  en reads "12 entries · Page 1/1" ("页 page" has no en remainder).
"""
from __future__ import annotations

EN_ENTRIES: dict[str, str] = {
    # ── board names / titles ──────────────────────────────────────────────
    "指标观察台": "Indicators",
    "爬虫控制台": "Crawl Control Center",
    "概念族谱": "Concept families",
    "关系": "Relations",
    "覆盖率": "Registry coverage",
    "关系图": "Interactive relation graph",
    "检索范围": "Retrieval scopes",
    "指标详情": "Indicator detail",

    # ── indicators.html (registry board intro) ────────────────────────────
    "统一指标注册目录与概念层关系的观察面：登记、族谱、跨源绑定、词表映射、覆盖率。":
        "Observability surface over the unified indicator registry and the"
        " concept layer — registrations, families, cross-source bindings,"
        " vocabulary mappings, coverage.",
    "与 MCP 工具同库同表读取（观测面，非分叉）。":
        "Reads the same tables the MCP tools serve.",

    # ── families / family detail ──────────────────────────────────────────
    "族代码": "Family code",
    "成员概念": "Member concepts",
    "绑定数": "Bindings",
    "说明": "Description",
    "暂无族": "No families.",
    "两级概念模型的第一级：族 → 成员变量。":
        "Two-level concept model, first level: families → member variables.",
    "点开族看成员与绑定数。":
        "Open a family for its members and their binding counts.",
    "族": "Family",
    "返回族谱": "back to families",
    "在关系图中打开": "open in graph",
    "该族暂无成员概念": "No member concepts.",
    "概念": "Concept",
    "实体类型": "Entity",
    "频率": "Freq",
    "单位": "Unit",

    # ── indicator (concept) detail ────────────────────────────────────────
    "实体": "entity",
    "口径": "measure",
    "返回登记表": "back to registry",
    "跨源等价对": "Cross-source equivalence (registry anchors)",
    "同一语义代码在各源库的锚点——合起来即该指标的跨源等价集。":
        "The registry anchors sharing this semantic code; together they are"
        " the indicator's cross-source equivalence set.",
    "源库": "Source db",
    "原生代码": "Native code",
    "注册目录中无该语义代码的锚点":
        "No registry anchors for this semantic code.",
    "本地绑定": "Local bindings (native codes)",
    "源": "Source",  # overrides core "Sources" — this board's header caliber
    "列（原生代码）": "Column (native code)",
    "置信度": "Confidence",
    "来源": "Provenance",
    "复核": "Reviewed",
    "暂无绑定": "No bindings.",
    "词表映射": "Vocabulary mappings",
    "词表": "Vocabulary",
    "词条": "Term",
    "暂无映射": "No mappings.",
    "未验证 — 不入公网目录":
        "eligible for the public catalog only once verified",

    # ── relations ─────────────────────────────────────────────────────────
    "跨源绑定等价对与词表映射的可检索清单。":
        "Searchable lists of cross-source bindings and vocabulary mappings.",
    "全部词表": "all vocabularies",
    "全部关系": "all relations",
    "全部源": "all sources",
    "关系类型": "relation type",
    "关键词": "keyword",
    "概念/词条关键词": "concept/term keyword",
    "筛选": "Filter",
    "无匹配映射": "No matching mappings.",
    "跨源绑定": "Cross-source bindings",
    "无匹配绑定": "No matching bindings.",

    # ── registry listing / filters / paging ───────────────────────────────
    "域": "Domain",
    "验证状态": "verified",
    "全部域": "all domains",
    "全部源库": "all sources",
    "全部（含未验证）": "all incl. unverified",
    "已验证": "Verified",
    "未验证": "Unverified",
    "名称/代码关键词": "name/code keyword",
    "注册目录表在本库不存在（本地开发库或未跑注册 DDL）——板块只观察，不影响服务。":
        "The registry table is not present on this database (local dev or"
        " the registry DDL has not run); the board only observes.",
    "条 entries": "entries",
    "第": "Page",
    "页 page": "",  # en remainder folded into "第" → "Page 1/1"
    "语义代码": "Semantic code",
    "无匹配条目": "No matching entries.",
    "分页": "pagination",

    # ── coverage ──────────────────────────────────────────────────────────
    "按源库与域的 registered / verified 统计；与 MCP 工具":
        "Registered / verified counts by source database and domain; the"
        " same GROUP BY the",
    "同库同 GROUP BY——同一时刻数字一致。":
        "MCP tool runs on the same database, so the two surfaces agree at"
        " the same moment.",
    "注册目录表在本库不存在——无法统计。":
        "The registry table is not present on this database; counts are"
        " unavailable.",
    "按源库": "By source database",
    "按域": "By domain",
    "按源库 registered 与 verified by source db":
        "Registered and verified by source database",
    "按域 registered 与 verified by domain":
        "Registered and verified by domain",
    "已登记": "Registered",
    "验证率": "Verified %",

    # ── graph ─────────────────────────────────────────────────────────────
    "按族 / 指标 / 域圈定的邻域图：平移、缩放、点选节点看详情。":
        "Neighborhood graph scoped by family / indicator / domain — pan,"
        " zoom, and select a node for its detail.",
    "视图规模有服务端上限。": "Views are bounded server-side.",
    "指标 id": "Indicator id",
    "深度": "depth",
    "关系图画布": "relation graph canvas",
    "节点详情": "Node detail",
    "点选一个节点查看详情": "Select a node to see its detail.",
    "关系列表（无脚本降级）": "Relation listing (no-script fallback)",
    "客户端脚本不可用——呈现服务端关系列表。":
        "Client scripting is unavailable; a server-rendered relation"
        " listing is served instead.",
    "族 → 成员概念": "Family → member concepts",
    "最近词表映射": "Latest vocabulary mappings",
    "完整关系数据可见于": "Full relation data is on the",
    "关系页": "Relations page",

    # ── scopes ────────────────────────────────────────────────────────────
    "scope = 命名白名单（源库/域/语义码/原生码四维，空维=不限）；panel 与 MCP scope 工具直调同一服务函数——此处创建的 scope 立即可供工具使用。":
        "A scope is a named allow-list over four dimensions; the panel"
        " calls the same service functions the MCP scope tools call, so"
        " anything created here is immediately usable via the tools.",
    "语义码": "semantic_codes",
    "原生码": "native_codes",
    "近7天命中": "Hits (7d)",
    "次": "calls",
    "行": "rows",
    "暂无 scope": "No scopes.",
    "创建": "Create",
    "scope 名称": "scope name",
    "（逗号分隔，留空=不限）": "(comma-separated, empty = unrestricted)",
    "空集校验：规则必须至少匹配一个已知指标（对照 registry_entries/concepts 活表），否则拒绝创建。":
        "Empty scopes are refused — rules must match at least one known"
        " indicator against the live tables.",
    "保留名": "Reserved name",
    "不可用。": "is unavailable.",
    "返回 scope 列表": "back to scopes",
    "命中统计": "Hit statistics",
    "天，合计": "days, totals:",
    "次调用": "calls",
    "行结果": "results",
    "日期": "Day",
    "调用次数": "Calls",
    "返回行数": "Results returned",
    "暂无命中记录": "No hit statistics yet.",
    "编辑规则": "Edit rules",
    "删除此 scope": "Delete this scope",
    "删除会同时解除调用方默认绑定；命中统计保留为历史。":
        "Deleting removes caller default bindings too; hit statistics"
        " remain as history.",
}
