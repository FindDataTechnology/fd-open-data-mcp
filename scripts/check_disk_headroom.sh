#!/usr/bin/env bash
# 磁盘余量告警：根分区可用空间低于阈值即非零退出。
#
# 为什么需要它：100.64.0.7 曾把根分区写到 100%，表现不是"磁盘满"这种直白错误，而是
# 该机 PostgreSQL 连进来就报
#   FATAL: could not write init file
# 进而让 semantic_observations_read（UNION ALL 了它的 fdw 外表）整个视图失败，
# 全平台读路径一起挂。**这种坏法不会自己喊**，只能靠余量探针提前叫。
#
# 判据取严（口径有歧义时选可验证更严的一侧）：
#   * 低于阈值          → 退出 1，并打印当时的真实数值
#   * df 读不到/解析不出 → 退出 2（**未知不等于正常**，绝不当通过）
#   * 全部达标          → 退出 0
#
# 用法：
#   ./check_disk_headroom.sh                      # 默认检查 /，阈值 1G
#   THRESHOLD_MB=2048 ./check_disk_headroom.sh    # 阈值 2G
#   MOUNTS="/ /var/lib/docker" ./check_disk_headroom.sh
#   ./check_disk_headroom.sh --warn-only          # 只打印不返回非零（用于观察期）
#
# 接 cron（宿主机）：  */10 * * * * /opt/fd/check_disk_headroom.sh || logger -t disk-headroom "low"
# 接 CronJob：把退出码交给 job 的 backoffLimit=0，失败即触发既有的告警投递链。

set -uo pipefail

THRESHOLD_MB=${THRESHOLD_MB:-1024}
MOUNTS=${MOUNTS:-/}
WARN_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --warn-only) WARN_ONLY=1 ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
    shift
done

THRESHOLD_KB=$(( THRESHOLD_MB * 1024 ))
WORST=0
EXIT=0

for m in $MOUNTS; do
    # -P：POSIX 输出，避免长设备名把列挤到下一行（df 的经典解析坑）
    if ! line=$(df -Pk "$m" 2>/dev/null | awk 'NR==2'); then
        echo "UNKNOWN $m: df 执行失败" >&2
        EXIT=2
        continue
    fi
    if [ -z "$line" ]; then
        echo "UNKNOWN $m: df 没有返回数据行（挂载点不存在？）" >&2
        EXIT=2
        continue
    fi

    # df -P 的列是 Filesystem / 1024-blocks / Used / Available / Capacity / Mounted on
    # ——第一列是设备名，从第 2 列起才是数。用 awk 按列取，不用位置参数：
    # `set --` 会覆盖脚本自己的 $@，且一旦拿错列（"$1"=设备名）后面的算术会炸。
    TOTAL_KB=$(echo "$line" | awk '{print $2}')
    USED_KB=$(echo "$line" | awk '{print $3}')
    AVAIL_KB=$(echo "$line" | awk '{print $4}')
    # 数值校验：解析不出来就是未知，不能当 0 也不能当正常
    for v in "$TOTAL_KB" "$USED_KB" "$AVAIL_KB"; do
        case "$v" in
            ''|*[!0-9]*) echo "UNKNOWN $m: 块数解析失败（'$line'）" >&2; EXIT=2; continue 2 ;;
        esac
    done

    PCT=$(( TOTAL_KB > 0 ? USED_KB * 100 / TOTAL_KB : 0 ))
    AVAIL_MB=$(( AVAIL_KB / 1024 ))

    if [ "$AVAIL_KB" -lt "$THRESHOLD_KB" ]; then
        echo "LOW $m: 可用 ${AVAIL_MB} MiB（${PCT}% 已用），低于阈值 ${THRESHOLD_MB} MiB —— 先扩盘，不要删数据卷"
        [ "$EXIT" -eq 0 ] && EXIT=1
    else
        echo "OK  $m: 可用 ${AVAIL_MB} MiB（${PCT}% 已用），阈值 ${THRESHOLD_MB} MiB"
    fi
done

if [ "$WARN_ONLY" -eq 1 ] && [ "$EXIT" -eq 1 ]; then
    echo "（--warn-only：低余量不返回非零）"
    exit 0
fi
exit "$EXIT"
