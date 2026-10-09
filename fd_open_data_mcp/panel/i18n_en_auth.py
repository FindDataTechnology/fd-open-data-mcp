"""EN entries for the auth / login-station / proxy template group
(panel-rbac-i18n-refresh task 4.3).

Keys are the templates' verbatim Chinese strings — ``t`` passes the key
through for zh (and for any en miss), so zh rendering is byte-stable while
the old inline paired-English (``<span class="en">`` twins, "zh 中文 en
english" run-ins) renders only through this dictionary in en. Keys with
``{n}``/``{s}``/``{a}`` placeholders are filled by ``.format(...)`` (or
``.replace(...)`` in the station modal's script) at the call site.

Merged into ``panel.i18n.EN`` by the glob loader in i18n.py; core nav /
common-button keys (登录, 操作, 状态 …) are reused, not redefined.
"""
from __future__ import annotations

EN_ENTRIES: dict[str, str] = {
    # ── auth page shell ──────────────────────────────────────────────────
    "认证身份池": "Auth identities",
    # shared chrome terms — values aligned with the shell/sources group files
    # so the merged dictionary stays merge-order-independent
    "爬虫控制台": "Crawl Control Center",
    "每源多账号身份池：五态身份、租借与会话健康、需登录队列与事件流。":
        "Per-source multi-account identity pool: five-state identities, "
        "leases, login queue, event stream.",
    "登录经登录站完成：从本页拉起登录站，在页内嵌入的观察窗中完成滑块/验证。":
        "Logins happen on a login station launched from this panel, "
        "completed inside the embedded observation view.",
    "每 {n}s 自动刷新。": "Refreshes every {n}s.",
    "加载中": "Loading",

    # ── standing alert banner (page slot #auth-alert + polled OOB update) ──
    "{n} 个身份需登录": "identities needing a login: {n}",
    "{n} 个身份会话可能已过期": "identities with possibly expired sessions: {n}",
    "点下方「登录」拉起登录站。":
        "use the Sign in buttons below to open a login station.",

    # ── summary badges + identity matrix ──────────────────────────────────
    "身份": "Identities",
    "活跃": "Active",
    "需登录": "Login required",
    "租借中": "Leased",
    "租约过期": "Lease expired",
    "认证源": "authenticated",
    "账号": "Account",
    "自动化": "Automation",
    "最近登录": "Last login",
    "最近成功": "Last success",
    "失败次数": "Failures",
    "连续零产出": "Zero-run streak",
    "租借": "Lease",
    "租借令牌": "Lease token",
    "尚无身份": "No identities registered yet.",
    "从未登录": "never logged in",

    # ── section headings ──────────────────────────────────────────────────
    "需登录队列": "Login-required queue",
    "新建账号": "New account",
    "身份矩阵": "Identity matrix",
    "事件流": "Event stream",
    "最近 20 条": "latest 20",
    "登录站": "Login stations",
    "站": "Station",
    "期限": "Deadline",

    # ── auth-brief short items ────────────────────────────────────────────
    "失败次数多者先处理，从未登录过的（新登记）排最前。":
        "Most failures first; never-logged-in registrations at the head.",
    "每行可直接拉起登录站，行内按钮即对该身份发起登录任务。":
        "Each row launches its login station inline.",
    "选源 + 起别名即登记身份，登记后进入上方队列等待登录。":
        "Pick a source + an alias to register an identity; it joins the "
        "queue above.",
    "出口可留自动分配，或自选一个固定出口（该账号的登录与后续爬取都走它）。":
        "Leave the egress on auto, or pin one explicitly (the account logs "
        "in and crawls through it).",
    "按源分组：账号 / 五态状态 / 自动化程度 / 最近登录 / 最近成功 / 连续零产出 / 租借。":
        "Grouped by source: account, five-state status, automation, last "
        "login/success, zero-run streak, lease.",
    "login_required 行内可直接拉起登录站（可换出口）。":
        "login_required rows launch a login station inline (egress "
        "switchable).",
    "login / probe / small_batch / lease / 失效与封禁留痕。":
        "The auditable login/probe/lease trail.",
    "可按身份回放。": "Replayable per identity.",
    "进行中/最近的登录站：状态、期限与观察窗入口。":
        "Live and recent stations: status, deadline, observation-view entry.",
    "超期未完成的站会被标记超时并可回收；已结束的站可一键重新拉起。":
        "An overdue station is marked timeout and reclaimable; a finished "
        "one relaunches in one click.",

    # ── queue rows, registration form, egress picker ──────────────────────
    "数据源": "Sources",
    "无认证源": "no auth_profile sources",
    "账号别名": "Account alias",
    "出口": "Egress",
    "登录与爬取的出口": "Egress for login + crawl",
    "出口：沿用当前": "Egress: keep current",
    "自动分配出口": "Egress: auto",
    "未绑定": "unbound",
    "拉起登录站": "Launch login station",
    "登记": "Register",
    "队列为空，没有需要登录的身份": "Empty queue — no identities need a login.",
    "尚无事件": "No identity events yet.",
    "暂无登录站": "No login stations yet.",

    # ── station row (two-step reclaim / relaunch) ─────────────────────────
    "观察窗": "Observation view",
    "回收": "Reclaim",
    "确认回收": "Confirm reclaim",
    "保留": "Keep",
    "重新拉起": "Relaunch",
    "重新拉起时使用的出口": "Egress for the relaunch",

    # ── observation modal (_station_view, static copy + poller script) ────
    "关闭": "Close",
    "在下方画面中输入该站的账号密码（如遇滑块 / 验证码请一并完成）。":
        "Type the account's credentials in the frame below (complete any "
        "slider/captcha too).",
    "登录成功后本窗口自动提示并关闭，无需手动刷新。":
        "On success this window announces it and closes itself — no manual "
        "refresh.",
    "拉起中：站桌面约需 20-40 秒出现，会自动重试，无需手动刷新。":
        "Booting: the desktop appears within ~40s and retries itself.",
    "该站已结束": "This station has finished",
    "观察通道已关闭。": "the observation channel is closed.",
    "失败原因": "Failure reason",
    "重新拉起登录站": "Relaunch login station",
    "登录成功": "Login captured",
    "身份 {s} / {a} 已激活，站点即将自动关闭。":
        "Identity {s} / {a} active — closing shortly.",
    "登录站结束": "Login station finished",
    "画面已就绪：请在上方完成登录。": "Ready — complete the login above.",

    # ── registration confirmation (_identity_created) ─────────────────────
    "已登记": "Registered",
    "未分配（出口池耗尽）": "none (egress pool exhausted)",

    # ── proxy / egress group ──────────────────────────────────────────────
    "代理与出口": "Proxy / Egress",
    "管理操作已禁用": "Management actions are disabled",
    "面板环境未设置": "not set in the panel environment",
    "本页面为只读": "This page is read-only",
    "下方的出口可观测性仍来自共享主库":
        "egress observability below still reflects the shared master DB",
    "出口健康（每 数据源 × 代理）": "Egress health (per source × proxy)",
    "冷表快照（近实时）": "Cold-table snapshot (near-realtime)",
    "热状态在": "hot state lives in",
    "提供商": "Providers",
    "提供商名称": "Provider name",
    "导入静态端点": "Import static endpoints",
    "端点列表，每行一个": "Endpoints — one per line",
    "导入": "Import",
    "没有提供商所有的代理行": "No provider-owned proxy rows.",
    "地址": "Addresses",
    "端点": "Endpoint",
    "凭证": "Credentials",
    "退役": "Retire",
    "重新启用": "Reactivate",
    "没有代理行": "No proxy rows.",
    "每源限速": "Per-source rate limits",
    "最大 QPS": "Max QPS",
    "并发": "Conc.",
    "并发上限": "Max concurrent",
    "关": "off",
    "未配置限速（默认 1 QPS）":
        "No rate limits configured (default 1 QPS applies).",
    "强制熔断重置": "Force circuit reset",
    "代理 ID": "Proxy ID",
    "操作员（可选）": "Operator (optional)",
    "重置为 HALF_OPEN": "Reset to HALF_OPEN",
    "强制 OPEN → HALF_OPEN，绕过冷却":
        "Forces OPEN → HALF_OPEN bypassing cooldown",
    "真实封禁会自动重新打开": "a real ban re-opens automatically",
    "封禁规则（只读）": "Ban rules (read-only)",
    "模式串": "Pattern",
    "分类": "Category",
    "没有注册封禁规则": "No ban rules registered.",
    "连败": "Fail streak",
    "熔断次数": "Cycles",
    "永久": "Permanent",
    "冷却至": "Cooldown",
    "暂无熔断状态": "No circuit state recorded yet.",
}
