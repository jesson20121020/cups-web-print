#!/bin/bash
# cups-web-print 开发服务器启动脚本
#
# 用法：
#   ./dev-server.sh           # 启动（默认端口 5000）
#   ./dev-server.sh 8080      # 自定义端口
#   ./dev-server.sh stop      # 停止
#   ./dev-server.sh status    # 查看状态
#   ./dev-server.sh restart   # 重启
#   ./dev-server.sh logs      # 查看日志
#
# 设计说明：
#   - cups-web-print 在工作区目录运行，提供端口 5000 的 Flask 服务
#   - dsh-web-preview-panel 通过 iframe 嵌入 localhost:5000
#   - 修改代码后用 restart 即可（Flask 不会自动 reload）

set -e

ACTION="${1:-start}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
LOG_FILE="/tmp/cups-web-print-dev.log"
PID_FILE="/tmp/cups-web-print-dev.pid"

# 端口：第一个数字参数是端口；命令参数不算
PORT=5000
for arg in "$@"; do
    if [[ "$arg" =~ ^[0-9]+$ ]]; then
        PORT="$arg"
        break
    fi
done

start() {
    # 检查端口
    if ss -tln 2>/dev/null | grep -q ":${PORT}\b"; then
        echo "⚠️  端口 $PORT 已被占用："
        ss -tlnp 2>/dev/null | grep ":${PORT}\b"
        return 1
    fi

    # 检查依赖
    if ! python3 -c "import flask, img2pdf" 2>/dev/null; then
        echo "❌ 缺少依赖，请先运行："
        echo "   pip install --break-system-packages flask werkzeug img2pdf"
        return 1
    fi

    # 启动
    echo "🚀 启动 cups-web-print 开发服务器（端口 $PORT）..."
    nohup python3 -u app.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    disown

    sleep 2
    if ss -tln 2>/dev/null | grep -q ":${PORT}\b"; then
        echo "✅ 启动成功！"
        echo "   浏览器：http://localhost:$PORT/zh"
        echo "   日志：tail -f $LOG_FILE"
        echo "   PID: $(cat $PID_FILE)"
    else
        echo "❌ 启动失败，请查看日志：$LOG_FILE"
        tail -20 "$LOG_FILE"
        return 1
    fi
}

stop() {
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "🛑 停止 cups-web-print (PID $PID)..."
            kill "$PID"
            sleep 1
        fi
        rm -f "$PID_FILE"
    fi
    # 兜底：清理所有 python3 app.py 进程
    pkill -f "python3.*app\.py" 2>/dev/null && echo "已清理残留进程" || true
}

status() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "✅ 运行中 (PID $(cat "$PID_FILE"))，端口 $PORT"
        ss -tlnp 2>/dev/null | grep ":${PORT}\b" | head -1
    else
        echo "❌ 未运行"
        ss -tln 2>/dev/null | grep ":${PORT}\b" || echo "   端口 $PORT 也未监听"
    fi
}

case "${ACTION}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        sleep 1
        start
        ;;
    status)
        status
        ;;
    logs)
        tail -f "$LOG_FILE"
        ;;
    *)
        echo "用法：$0 {start|stop|restart|status|logs} [port]"
        exit 1
        ;;
esac