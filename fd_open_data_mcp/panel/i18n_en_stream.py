"""Stream-group EN entries (add-data-stream-engine task 5.3 stream board).

Merged into ``i18n.EN`` by the loader (sorted glob). Keys are the Chinese
literals used in ``stream.html`` / the nav link.
"""
EN_ENTRIES = {
    # nav + title
    "流面板": "Stream board",
    "流面板 Stream board · 柏讯寻新": "Stream board · Wire Scout",
    "数据流获取与处理的运行健康（只读）read-only runtime health of the stream plane":
        "Read-only runtime health of the stream plane",
    # stream health
    "流面健康度": "Stream health",
    "未配置 Redis 流面，无健康度数据（仅展示存储水位与执行分布）":
        "No Redis stream configured — health unavailable (showing storage watermark and execution distribution only)",
    "流面不可达": "Stream unreachable",
    "积压": "Backlog",
    "未确认": "Pending",
    "最老滞后": "Oldest lag",
    "消费者": "Consumers",
    "告警": "Alert",
    "正常": "OK",
    # storage watermark
    "存储水位": "Storage watermark",
    "覆盖概念": "Covered concepts",
    "覆盖点": "Covered points",
    "跨源共存": "Coexisting points",
    "rollup 新鲜度": "Rollup freshness",
    "hook 健康": "Hook health",
    "健康": "Healthy",
    "漂移": "Drift",
    "本地存量": "Local rows",
    "行": "rows",
    "obs_month 桶": "obs_month buckets",
    "个月": "months",
    "default 分区收容": "default partition",
    "obs_month 分布未采样": "obs_month distribution not sampled",
    "census 尚未采样（存储存量与 obs_month 分布待刷新）":
        "Census not sampled yet (storage volume and obs_month distribution pending a refresh)",
    # execution distribution
    "执行模型分布": "Execution model distribution",
    "近": "last",
    "次运行": "runs",
    "租约式 leasing": "Leasing",
    "作业式 job": "Job",
}
