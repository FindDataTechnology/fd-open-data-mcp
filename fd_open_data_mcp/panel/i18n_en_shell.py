"""Shell-group EN entries (panel-rbac-i18n-refresh task 4.3 shell wave).

Covers: base.html chrome + brand, home board + all home partials, runs
board trio, fleet/platform/pending rows, macros defaults, denied page.
Merged into ``i18n.EN`` by the loader in i18n.py (sorted glob order; keys
shared with other groups carry the same value).
"""
EN_ENTRIES = {
    # brand (spec panel-i18n: 对外命名口径)
    "柏讯·寻新": "Wire Scout",
    # base chrome
    "跳到内容": "Skip to content",
    "总览": "Home",
    "平台源": "Sources",
    "候选源": "Funnel",
    "新建策略": "New policy",
    "切换语言": "Switch language",
    "切换主题": "Toggle theme",
    # home board
    "现在与接下来": "Now & next",
    "调度器静默": "Scheduler quiet",
    "调度器静默说明": "no run has started in the last",
    "上次": "last",
    "从未记录过运行": "no runs ever recorded",
    "reconciler 可能已挂起或没有任何调度": "the reconciler may be suspended or nothing is scheduled",
    "24h 成功率": "24h success rate",
    "24h 新增行": "24h new rows",
    "24h 失败": "24h failures",
    "产出趋势（近 14 天）": "Yield trend (last 14 days)",
    "近 14 天每日产出条形图": "daily yield, last 14 days",
    "舰队": "Fleet",
    "平台源健康": "Platform sources",
    "运行中的爬取": "Running runs",
    "接下来": "Next up",
    "最近完成": "Recent finished",
    "加载中": "Loading",
    "错过的运行": "Missed runs",
    # runs board
    "平台运行": "Platform runs (crawl_runs)",
    "平台运行说明": ("Runs reported to crawl_runs by every platform site, shown side "
                     "by side with the policy runs above; the Trigger column links "
                     "to the source detail."),
    "产出行": "Rows",
    "质量": "Quality",
    "触发行": "Trigger",
    "错误": "Error",
    "暂无平台运行": "No platform runs.",
    "全部策略": "all policies",
    "预估抓取": "Est. fetches",
    "任务": "Job",
    "详情": "Detail",
    "暂无运行": "No runs.",
    "确认取消": "Confirm cancel",
    "保留": "Keep",
    # running partial
    "位置": "Where",
    "已用时": "Elapsed",
    "进度": "Progress",
    "已试": "att.",
    "新增": "new",
    "尚未上报产出": "pod has not reported yield yet",
    "暂无运行中的爬取": "Nothing running.",
    # recent partial
    "产出": "Yield",
    "完成于": "Finished",
    "未上报": "not reported",
    "新": "new",
    "试": "att.",
    "还没有运行": "No runs yet.",
    # next partial
    "下次触发（本地）": "Next fire (local)",
    "还有": "In",
    "暂无启用的策略": "No enabled policies.",
    # missed partial
    "错过的触发": "Missed fire",
    "晚了": "Late by",
    "原因": "Reason",
    "没有错过的运行": "No missed runs.",
    # platform partial
    "源总数": "Sources",
    "已点亮": "Lit",
    "停滞": "Stalled",
    "24h 成功": "24h ok",
    "24h 失败": "24h failed",
    # fleet partial + rows
    "集群": "Cluster",
    "并发 开/上限": "Open/Cap",
    "任务（实时）": "Jobs (live)",
    "可达": "Reachable",
    "出口": "Egress",
    "标签": "Tags",
    "没有注册的集群": "No clusters registered.",
    "done 星注": ("* done* = the Job exited but its run row is not closed yet "
                  "(reconciler lag)."),
    "（已停用）": "(disabled)",
    "最大并发运行数，下个 tick 生效": "max concurrent open runs — effective next tick",
    "最大并发运行数": "max concurrent runs",
    "保存容量": "save capacity",
    "空闲": "idle",
    "超载": "over",
    "正常": "up",
    "不可达": "unreachable",
    # platform/pending rows
    "本次运行所租身份": "the leased identity",
    "取消请求中": "cancelling",
    # macros defaults
    "柱状图": "bar chart",
    "分组柱状图": "grouped bar chart",
    "趋势线": "sparkline",
    "抓取时间线": "fetch timeline",
    "分布图": "distribution",
    # denied page
    "权限不足": "Access denied",
    "无权访问": "No access",
    "无权访问说明前": "Your current role does not grant access to",
    "该页面": "this page",
    "无权访问说明后": "",
    "无权访问提示": ("Contact an administrator to adjust your roles in Logto if you "
                     "need higher permissions."),
    "返回总览": "Back to home",
    # py-side chrome (app.py _t() users)
    "退出": "Logout",
    "未分配": "none",
    "capacity 必须是整数": "capacity must be an integer",
    "capacity 必须 >= 0": "capacity must be >= 0",
}
