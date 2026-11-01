#!/bin/bash
# cups-usb-watchdog —— 修复「USB 打印机不在总线上，打印任务永远卡住」的卡死状态
#
# 背景（2026-11-01 实战）：
#   打印机 USB 掉线后内核重新枚举失败（xhci 报 "Cannot enable. Maybe the USB cable
#   is bad?" → "attempt power cycle" → "unable to enumerate USB device"），之后内核
#   就不再重试该端口；于是：
#     - lsusb 里没有这台打印机
#     - ipp-usb 服务起不来（没有设备可绑定），127.0.0.1:60000-60010 全部无人监听
#     - CUPS 任务停在 “now printing”，每秒重试都只是 “Host is down”
#     - 无论从网页点多少次打印都“唤醒”不了打印机（请求根本到不了设备）
#   唯一有效的恢复动作是给该 USB 端口做一次 disable/enable（端口重新上电探测），
#   设备立刻重新枚举、ipp-usb 起来、卡住的任务会自动接着打印。
#   —— 该动作需要 root，所以放在这个 root 定时器里做。
#
# 安全边界：只有「有排队任务 + 完全没有 ipp-usb 端点 + 总线找不到打印机 USB 设备」
# 三条同时成立才动作；且只复位“上次打印机所在的那个端口”，该端口若已有别的设备
# 枚举成功则绝不触碰。冷却时间内不重复复位。
#
# 用法：
#   cups-usb-watchdog.sh            # 正常巡检（systemd timer 调用）
#   cups-usb-watchdog.sh --status   # 打印当前诊断信息，不动作
#   cups-usb-watchdog.sh --dry-run  # 走完判断逻辑，但不真的复位端口

set -u

LOG_FILE="${CUPS_USB_WATCHDOG_LOG:-/var/log/cups-usb-watchdog.log}"
STATE_DIR="${CUPS_USB_WATCHDOG_STATE:-/var/lib/cups-usb-watchdog}"   # 持久化，重启后仍知道打印机在哪个口
COOLDOWN="${CUPS_USB_WATCHDOG_COOLDOWN:-300}"   # 两次复位的最小间隔（秒）
ENDPOINT_PORTS="60000 60001 60002 60003 60004 60005 60006 60007 60008 60009 60010"

MODE="run"
case "${1:-}" in
    --dry-run) MODE="dry-run" ;;
    --status)  MODE="status" ;;
    "")        ;;
    *) echo "用法: $0 [--dry-run|--status]" >&2; exit 2 ;;
esac

log() {
    local line="[$(date '+%F %T')] $*"
    echo "$line"
    [ "$MODE" = "run" ] && echo "$line" >> "$LOG_FILE"
}

die() { log "ERROR: $*"; exit 1; }

mkdir -p "$STATE_DIR" 2>/dev/null || true

# 日志自限：超过 500 行只保留最近 200 行（事件很稀疏，一般不会触发）
if [ "$MODE" = "run" ] && [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)" -gt 500 ]; then
    tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
fi

# ---------- 1. 总线上找 USB 打印机（bInterfaceClass = 07 = Printer） ----------
find_printer_devpaths() {
    local f dev
    for f in /sys/bus/usb/devices/*/bInterfaceClass; do
        [ -e "$f" ] || continue
        [ "$(cat "$f" 2>/dev/null)" = "07" ] || continue
        dev="$(basename "$(dirname "$f")")"   # 形如 1-5:1.1
        echo "${dev%%:*}"                    # → 1-5
    done
}

# devpath(1-5) → 端口控制目录（/sys/.../usb1/1-0:1.0/usb1-port5）
port_dir_of() {
    local devpath="$1" bus rest port
    case "$devpath" in
        *.*|"") return 1 ;;                 # 挂在 hub 上的暂不处理（保持安全边界）
    esac
    bus="${devpath%%-*}"
    rest="${devpath#*-}"
    port="${rest%%.*}"
    [ -n "$bus" ] && [ -n "$port" ] || return 1
    echo "/sys/bus/usb/devices/usb${bus}/${bus}-0:1.0/usb${bus}-port${port}"
}

# ---------- 2. ipp-usb 端点探测（不依赖 ipptool） ----------
endpoint_alive() {
    local p
    for p in $ENDPOINT_PORTS; do
        if (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then
            exec 3>&- 2>/dev/null
            return 0
        fi
    done
    return 1
}

# ---------- 3. 状态汇总 ----------
mapped_devs="$(find_printer_devpaths | sort -u | tr '\n' ' ')"
mapped_devs="${mapped_devs% }"
ep_alive=0
endpoint_alive && ep_alive=1
pending_jobs="$(lpstat -o 2>/dev/null | wc -l)"
last_port_file="$STATE_DIR/last_port"
last_port="$(cat "$last_port_file" 2>/dev/null || true)"
last_reset="$(cat "$STATE_DIR/last_reset" 2>/dev/null || echo 0)"
now="$(date +%s)"

# 自检：模拟“打印机不在总线 + 端点无监听 + 有排队任务”（便于验证触发逻辑，不碰硬件）
if [ "${CUPS_USB_WATCHDOG_SELFTEST:-0}" = "1" ]; then
    mapped_devs=""
    ep_alive=0
    pending_jobs=1
fi

endpoint_state="无（全部未监听）"
[ "$ep_alive" = "1" ] && endpoint_state="有（${ENDPOINT_PORTS%% *} 起范围内存在监听）"

if [ "$MODE" = "status" ]; then
    echo "USB 打印机设备: ${mapped_devs:-无}"
    echo "ipp-usb 端点:   $endpoint_state"
    echo "排队任务数:     $pending_jobs"
    echo "上次端口:       ${last_port:-未记录}"
    echo "上次复位:       $([ "$last_reset" -gt 0 ] && date -d "@$last_reset" '+%F %T' || echo 从未)"
    exit 0
fi

# ---------- 4. 记住打印机所在端口（每次看到都在跑，便于掉线后知道复位谁） ----------
if [ -n "$mapped_devs" ]; then
    for devpath in $mapped_devs; do
        pd="$(port_dir_of "$devpath")" || continue
        [ -d "$pd" ] || continue
        prev_port="$(cat "$last_port_file" 2>/dev/null || true)"
        echo "$pd" > "$last_port_file"
        # 只在端口变化时记一行，避免每 2 分钟刷日志
        if [ "$MODE" = "run" ] && [ "$prev_port" != "$pd" ]; then
            log "检测到 USB 打印机：$devpath（端口 $pd），ipp-usb 端点 $endpoint_state"
        fi
        break
    done
    exit 0
fi

# ---------- 5. 触发判断 ----------
[ "$pending_jobs" -gt 0 ] || exit 0                       # 没任务就不折腾
if [ "$ep_alive" = "1" ]; then exit 0; fi                  # 端点活着，交给 ipp-usb/CUPS
if [ "$last_port" = "" ] || [ ! -d "$last_port" ]; then
    log "打印机不在总线上，但还没有记录过端口，无法自动复位（请手动插拔一次 USB 线）"
    exit 0
fi
port_state="$(cat "$last_port/state" 2>/dev/null || echo unknown)"
if [ "$port_state" = "configured" ]; then
    log "端口 $(basename "$last_port") 上已有其他设备在用（state=configured），跳过复位"
    exit 0
fi
if [ $((now - last_reset)) -lt "$COOLDOWN" ]; then
    log "打印机仍不在总线上，距上次复位不足 ${COOLDOWN}s，暂不重复动作"
    exit 0
fi

log "检测到卡死：有 ${pending_jobs} 个打印任务、无 ipp-usb 端点、总线无打印机（端口 state=${port_state}），开始复位 $(basename "$last_port")"
if [ "$MODE" = "dry-run" ]; then
    log "dry-run：跳过实际复位"
    exit 0
fi

echo "$now" > "$STATE_DIR/last_reset"
echo 1 > "$last_port/disable" 2>/dev/null || log "写 disable=1 失败"
sleep 1
echo 0 > "$last_port/disable" 2>/dev/null || log "写 disable=0 失败"
sleep 5

if find_printer_devpaths | grep -q .; then
    log "复位成功：打印机已重新枚举，ipp-usb/CUPS 会自动接上并继续打印排队任务"
else
    log "复位后仍未见设备：请检查打印机电源、USB 线（内核曾提示可能线缆不良），或手动拔插一次"
fi
exit 0
