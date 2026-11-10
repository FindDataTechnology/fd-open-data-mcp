#!/usr/bin/env bash
# 把云盘扩容后新增的空间并入根分区并扩展文件系统。
#
# 背景：100.64.0.7（VM-0-13-debian）根分区 39.5G 曾打到 100%、可用 0，
# 导致该机上的 PostgreSQL 在建立连接时直接报
#   FATAL: could not write init file
# 而 semantic_observations_read 视图 UNION ALL 了挂在它上面的 fdw 外表
# ft_sem_obs_xinru3，于是**全平台读路径一起失败**。
# 详见 openspec/changes/archive/2026-11-10-make-observation-vintage-readable/
#      host-disk-decision-2026-11-10.md
#
# 设计取舍：
#   * **默认 dry-run**，只有 --apply 才动磁盘。扩容是不可逆操作（ext4 不能在线缩容）。
#   * 只在"分区是磁盘上最后一个分区"时才做，别的布局一律拒绝——growpart 对非末尾
#     分区会搬数据，风险完全不同。
#   * 磁盘没有新增空间时明确报「先去控制台扩盘」，而不是假装成功。
#   * 判据取严：任何前置条件查不出来（不是"查出来没问题"）都退出，不猜。
#
# 用法：
#   ./expand_root_fs.sh                 # 只看计划，不改
#   ./expand_root_fs.sh --apply         # 真执行
#   DEVICE=/dev/sda PARTNUM=2 ./expand_root_fs.sh
#
# 退出码：0 成功或无事可做 / 1 前置条件不满足 / 3 执行后复核发现没长

set -uo pipefail

DEVICE=${DEVICE:-/dev/vda}
PARTNUM=${PARTNUM:-3}
MOUNT=${MOUNT:-/}
MIN_GROW_MB=${MIN_GROW_MB:-1024}   # 小于 1G 不值得折腾
APPLY=0

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply)   APPLY=1 ;;
        --device)  DEVICE=${2:?--device 需要参数}; shift ;;
        --partnum) PARTNUM=${2:?--partnum 需要参数}; shift ;;
        --mount)   MOUNT=${2:?--mount 需要参数}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 1 ;;
    esac
    shift
done

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "  $*"; }

echo "== 扩容前置检查 =="

[ "$(id -u)" -eq 0 ] || die "需要 root（growpart / resize2fs 都要）"
[ -b "$DEVICE" ] || die "$DEVICE 不是块设备（这台机上没有？）"

PART="${DEVICE}${PARTNUM}"
[ -b "$PART" ] || die "$PART 不存在"

# 文件系统类型：只为 ext2/3/4 写，xfs 走 xfs_growfs，别的直接拒绝。
FSTYPE=$(findmnt -no FSTYPE --target "$MOUNT" 2>/dev/null)
[ -n "$FSTYPE" ] || die "查不到 $MOUNT 的文件系统类型（未知不等于安全，拒绝继续）"
say "挂载点 $MOUNT → $FSTYPE"
case "$FSTYPE" in
    ext2|ext3|ext4) RESIZE_CMD="resize2fs $PART" ;;
    xfs)            RESIZE_CMD="xfs_growfs $MOUNT" ;;
    *)              die "不支持的文件系统 $FSTYPE（本脚本只覆盖 ext* / xfs）" ;;
esac

# 只读挂载时 resize 会失败，提前说清楚。
OPTS=$(findmnt -no OPTIONS --target "$MOUNT" 2>/dev/null)
case ",$OPTS," in *,ro,*) die "$MOUNT 当前是只读挂载，先处理只读原因再扩容" ;; esac

# 必须是磁盘上的最后一个分区，否则 growpart 会移动后续分区。
LAST=$(lsblk -lno NAME,TYPE "$DEVICE" 2>/dev/null | awk '$2=="part"{n=$1} END{print n}')
[ -n "$LAST" ] || die "在 $DEVICE 上找不到任何分区"
[ "$LAST" = "$(basename "$PART")" ] || \
    die "$PART 不是最后一个分区（最后一个是 $LAST）——非末尾分区扩容会搬数据，拒绝执行"

# 磁盘上是否真有未分配空间：总扇区 − 分区起点 − 分区扇区数。
SYSBLK=/sys/class/block/$(basename "$PART")
[ -r "$SYSBLK/start" ] && [ -r "$SYSBLK/size" ] || die "读不到 $SYSBLK/{start,size}"
DISK_SECTORS=$(blockdev --getsz "$DEVICE" 2>/dev/null) || die "blockdev 读不到 $DEVICE 扇区数"
PART_START=$(cat "$SYSBLK/start")
PART_SECTORS=$(cat "$SYSBLK/size")
FREE_SECTORS=$(( DISK_SECTORS - PART_START - PART_SECTORS ))

say "磁盘 $(basename "$DEVICE") 共 $(( DISK_SECTORS / 2048 )) MiB"
say "分区 $PART 占用 $(( PART_SECTORS / 2048 )) MiB，起点扇区 $PART_START"

if [ "$FREE_SECTORS" -eq 0 ] || [ $(( FREE_SECTORS / 2048 )) -lt 64 ]; then
    echo
    echo "磁盘基本已铺满（可并入 $(( FREE_SECTORS / 2048 )) MiB），没有实质新空间。"
    echo "→ 请先在云控制台把云硬盘 $DEVICE 扩容，再回来跑本脚本。"
    echo "  控制台扩容不可由宿主机代替，这一步必须人工。"
    exit 1
fi

FREE_MB=$(( FREE_SECTORS / 2048 ))
say "可并入空闲空间 ${FREE_MB} MiB"

if [ "$FREE_MB" -lt "$MIN_GROW_MB" ]; then
    echo
    echo "可并入空间 ${FREE_MB} MiB 小于阈值 ${MIN_GROW_MB} MiB，判定不值得动。"
    echo "→ 要么在控制台扩更多，要么用 MIN_GROW_MB=0 强制。"
    exit 1
fi

BEFORE_AVAIL_KB=$(df -Pk "$MOUNT" | awk 'NR==2{print $4}')
echo
echo "== 计划 =="
say "1) growpart $DEVICE $PARTNUM      （吸收 ${FREE_MB} MiB）"
say "2) $RESIZE_CMD"
say "扩容前可用：$(( BEFORE_AVAIL_KB / 1024 )) MiB"

if [ "$APPLY" -ne 1 ]; then
    echo
    echo "（dry-run：以上只是计划，未改动任何东西。加 --apply 才执行。）"
    exit 0
fi

command -v growpart >/dev/null 2>&1 || \
    die "缺 growpart。Debian/Ubuntu: apt-get install -y cloud-guest-utils"

echo
echo "== 执行 =="
growpart "$DEVICE" "$PARTNUM" || die "growpart 失败（分区未改动或部分改动，先看 df/parted 再决定）"
say "growpart 完成"
# shellcheck disable=SC2086
$RESIZE_CMD || die "resize 失败：分区已变大但文件系统没扩，重跑本脚本的 resize 步骤即可（幂等）"
say "文件系统已扩展"

echo
echo "== 复核 =="
df -hT "$MOUNT"
AFTER_AVAIL_KB=$(df -Pk "$MOUNT" | awk 'NR==2{print $4}')
GROWN_KB=$(( AFTER_AVAIL_KB - BEFORE_AVAIL_KB ))
say "可用空间变化：$(( BEFORE_AVAIL_KB / 1024 )) MiB → $(( AFTER_AVAIL_KB / 1024 )) MiB（+$(( GROWN_KB / 1024 )) MiB）"

if [ "$GROWN_KB" -le 0 ]; then
    echo "ERROR: 执行完可用空间没有增加，扩容没有生效" >&2
    exit 3
fi

echo
echo "扩容完成。建议紧接着跑一次存储自检："
echo "  docker exec fd-postgres psql -U fd -d fd_open_data -At \\"
echo "    -c 'create temp table _w(i int); insert into _w values (1); select count(*) from _w'"
exit 0
