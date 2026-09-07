#!/bin/bash
set -e

# Build cups-web-print .fpk package.
#
# Usage:
#   ./scripts/build-fpk.sh                         # 默认参数
#   ./scripts/build-fpk.sh [version] [platform] [fpk_version]
#
# 输出在 fnos-build/ 根目录：
#   cups-web-print_<version>_<platform>.fpk
#
# 流程：
#   1. 校验必需文件/目录
#   2. 把 app/ 打成 app.tgz，放在 fpk 顶层
#   3. 把 cmd/、config/、wizard/、manifest、ICON*.PNG 平铺到 fpk 顶层
#   4. 写入 manifest 的 checksum（md5(app.tgz)）+ 可选覆盖字段
#   5. 打包成 <appname>_<version>_<platform>.fpk
#
# fnpack 官方行为（验证自 techysy/deepseek-harness-fnos 真实 fpk）：
#   - fpk 顶层包含 app.tgz、cmd/、config/、wizard/、manifest、ICON*.PNG
#   - app.tgz 内顶层为 server/、ui/...
#   - 安装时 fnOS 将 app.tgz 解压到 ${TRIM_APPDEST}/target/

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

VERSION_OVERRIDE="${1:-}"
PLATFORM_OVERRIDE="${2:-}"
FPK_VERSION_OVERRIDE="${3:-}"

get_manifest_value() {
    local key="$1"
    grep -E "^${key}[[:space:]]*=" "${APP_DIR}/manifest" | head -1 | awk -F'=' '{print $2}' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

set_manifest_value() {
    local key="$1"
    local value="$2"
    local pad
    pad=$(printf '%*s' "$((24 - ${#key}))" '')
    sed -i.tmp "s|^${key}[[:space:]]*=.*|${key}${pad}= ${value}|" "${APP_DIR}/manifest"
    rm -f "${APP_DIR}/manifest.tmp"
}

# 必需文件检查
[ -f "${APP_DIR}/manifest" ]       || error "缺少 manifest"
[ -f "${APP_DIR}/ICON.PNG" ]       || error "缺少 ICON.PNG"
[ -f "${APP_DIR}/ICON_256.PNG" ]   || error "缺少 ICON_256.PNG"
[ -d "${APP_DIR}/cmd" ]            || error "缺少 cmd/"
[ -d "${APP_DIR}/config" ]         || error "缺少 config/"
[ -d "${APP_DIR}/app" ]            || error "缺少 app/"
[ -d "${APP_DIR}/app/server" ]     || error "缺少 app/server/"
[ -d "${APP_DIR}/wizard" ]         || error "缺少 wizard/"

APPNAME=$(get_manifest_value "appname")
VERSION=$(get_manifest_value "version")
PLATFORM=$(get_manifest_value "platform")
SOURCE=$(get_manifest_value "source")

[ -n "${APPNAME}" ]      || error "manifest 缺少 appname"
[ -n "${VERSION}" ]      || error "manifest 缺少 version"
[ -n "${SOURCE}" ]       || error "manifest 缺少 source"

# 打包临时目录
WORK_DIR=$(mktemp -d)
PKG_DIR="${WORK_DIR}/package"
APP_TGZ_TMP="${WORK_DIR}/app.tgz"
mkdir -p "${PKG_DIR}"

# 1. 打 app.tgz（在 app/ 内执行，使 tar 内顶层为 server/、ui/...）
#    注意：必须 -C 到 app/，让 tar 的条目以 server、ui、... 开头
(cd "${APP_DIR}/app" && tar -czf "${APP_TGZ_TMP}" .)

APP_TGZ_SIZE=$(stat -c%s "${APP_TGZ_TMP}" 2>/dev/null || stat -f%z "${APP_TGZ_TMP}")
[ "${APP_TGZ_SIZE}" -ge 10240 ] || error "app.tgz 过小 (${APP_TGZ_SIZE} bytes) — 可能已损坏"

CHECKSUM=$(md5sum "${APP_TGZ_TMP}" | cut -d' ' -f1)
info "checksum (md5 of app.tgz) = ${CHECKSUM}"
cp "${APP_TGZ_TMP}" "${PKG_DIR}/app.tgz"

# 2. 平铺其余内容
cp "${APP_DIR}/manifest"          "${PKG_DIR}/manifest"
cp "${APP_DIR}/ICON.PNG"          "${PKG_DIR}/ICON.PNG"
cp "${APP_DIR}/ICON_256.PNG"      "${PKG_DIR}/ICON_256.PNG"
[ -d "${APP_DIR}/cmd" ]    && cp -a "${APP_DIR}/cmd"    "${PKG_DIR}/"
[ -d "${APP_DIR}/config" ] && cp -a "${APP_DIR}/config" "${PKG_DIR}/"
[ -d "${APP_DIR}/wizard" ] && cp -a "${APP_DIR}/wizard" "${PKG_DIR}/"

# 端口转发 .sc（如果有）
if ls "${APP_DIR}"/*.sc 2>/dev/null | head -1 | grep -q .; then
    cp "${APP_DIR}"/*.sc "${PKG_DIR}/"
fi

# health.json
[ -f "${APP_DIR}/health.json" ] && cp "${APP_DIR}/health.json" "${PKG_DIR}/health.json"

# 3. 写入 manifest 的 checksum + 可选覆盖
set_manifest_value "checksum" "${CHECKSUM}"

if [ -n "${VERSION_OVERRIDE}" ]; then
    set_manifest_value "version" "${VERSION_OVERRIDE}"
    VERSION="${VERSION_OVERRIDE}"
fi

if [ -n "${PLATFORM_OVERRIDE}" ]; then
    set_manifest_value "platform" "${PLATFORM_OVERRIDE}"
    PLATFORM="${PLATFORM_OVERRIDE}"
fi

if [ -n "${FPK_VERSION_OVERRIDE}" ]; then
    set_manifest_value "fpk_version" "${FPK_VERSION_OVERRIDE}"
fi

# 把更新后的 manifest 也覆盖回 PKG_DIR
cp "${APP_DIR}/manifest" "${PKG_DIR}/manifest"

# 兜底校验
cd "${PKG_DIR}"
[ -f "manifest" ]      || error "打包失败：manifest 缺失"
[ -f "app.tgz" ]       || error "打包失败：app.tgz 缺失"
[ -d "cmd" ]           || error "打包失败：cmd/ 缺失"
[ -d "config" ]        || error "打包失败：config/ 缺失"
[ -d "wizard" ]        || error "打包失败：wizard/ 缺失"
[ -f "ICON.PNG" ]      || error "打包失败：ICON.PNG 缺失"
[ -f "ICON_256.PNG" ]  || error "打包失败：ICON_256.PNG 缺失"

# 输出
FPK_NAME="${APPNAME}_${VERSION}_${PLATFORM:-x86}.fpk"
tar -czf "${APP_DIR}/${FPK_NAME}" *

cd "${APP_DIR}"
rm -rf "${WORK_DIR}"

info "Built: ${FPK_NAME} ($(du -h "${APP_DIR}/${FPK_NAME}" | cut -f1))"
echo "${FPK_NAME}"