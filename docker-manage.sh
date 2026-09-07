#!/bin/bash
#
# cups-web-print Docker 管理脚本
#
# 用法：
#   ./docker-manage.sh start    # 启动容器
#   ./docker-manage.sh stop     # 停止容器
#   ./docker-manage.sh restart  # 重启容器
#   ./docker-manage.sh status   # 查看状态
#   ./docker-manage.sh logs     # 跟踪日志
#   ./docker-manage.sh update   # 拉取最新镜像并重启
#   ./docker-manage.sh shell    # 进入容器 shell
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONTAINER_NAME="cups-web-print"
IMAGE="ghcr.io/wishday/cups-web-print:latest"

start() {
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "⚠️  容器 ${CONTAINER_NAME} 已存在，先 stop..."
        stop
    fi

    if command -v docker-compose &>/dev/null; then
        docker-compose up -d
    else
        docker compose up -d
    fi
    sleep 3
    status
}

stop() {
    if command -v docker-compose &>/dev/null; then
        docker-compose down 2>/dev/null || docker rm -f ${CONTAINER_NAME} 2>/dev/null || true
    else
        docker compose down 2>/dev/null || docker rm -f ${CONTAINER_NAME} 2>/dev/null || true
    fi
    echo "✅ 已停止"
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "✅ 运行中"
        docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    else
        echo "❌ 未运行"
    fi
}

logs() {
    docker logs -f ${CONTAINER_NAME} 2>&1
}

update() {
    echo "📥 拉取最新镜像..."
    docker pull ${IMAGE}
    echo "🔄 重启容器..."
    restart
}

shell() {
    docker exec -it ${CONTAINER_NAME} /bin/bash
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) restart ;;
    status)  status ;;
    logs)    logs ;;
    update)  update ;;
    shell)   shell ;;
    *)
        echo "用法：$0 {start|stop|restart|status|logs|update|shell}"
        exit 1
        ;;
esac
