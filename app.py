#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web 打印服务 - 基于 Python Flask + CUPS
支持文档/图片上传、打印设置、队列管理
"""

from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import urllib.request
import subprocess
import json
import uuid
from datetime import datetime
import threading
import time
import logging
import re
import shutil
import glob
import tempfile
from urllib.parse import urlparse

# 应用版本号：从根目录 version 文件读取（纯数字，如 1.8），显示时加 v 前缀
def _read_version():
    version_file = os.path.join(os.path.dirname(__file__), 'version')
    try:
        with open(version_file, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return 'unknown'

APP_VERSION = _read_version()
DISPLAY_VERSION = f"v{APP_VERSION}" if APP_VERSION != 'unknown' else 'unknown'

# ---- 更新检查 ----
# 远程版本来源：GitHub 仓库根目录的 version 文件（与本地格式一致，纯数字）
# GitHub raw 为权威源（无 CDN 缓存，始终最新）；jsDelivr CDN 仅作降级备用
GITHUB_RAW_VERSION_URL = 'https://raw.githubusercontent.com/wishday/cups-web-print/main/version'
GITHUB_CDN_VERSION_URL = 'https://cdn.jsdelivr.net/gh/wishday/cups-web-print@main/version'
UPDATE_CHECK_TIMEOUT = 10  # 单次请求超时（秒）
UPDATE_CHECK_RETRIES = 3   # 失败最大重试轮数（每轮先取权威源，再取 CDN 兜底）
UPDATE_CHECK_BACKOFF = 1   # 重试间隔（秒）

# 更新检查状态（模块级，线程安全）
update_check = {
    'status': 'idle',          # idle / checking / up-to-date / update-available / error
    'latest': None,            # 远程最新版本（纯数字）
    'checked_at': None,        # 检查完成时间
    'error': None,             # 失败原因
}
update_check_lock = threading.Lock()


def _parse_version_tuple(version):
    """将版本号解析为整数元组用于比较，如 '1.10' -> (1, 10)"""
    if not version:
        return ()
    return tuple(int(x) for x in re.findall(r'\d+', str(version)))


def _fetch_version_from(url):
    """请求单个源获取版本号；成功返回纯数字字符串，失败或格式无效返回 None"""
    try:
        with urllib.request.urlopen(url, timeout=UPDATE_CHECK_TIMEOUT) as resp:
            text = resp.read().decode('utf-8').strip()
        if re.fullmatch(r'\d+(\.\d+)*', text):
            return text
        logger.warning(f"远程版本号格式无效：{url} -> {text!r}")
    except Exception as e:
        logger.warning(f"获取远程版本失败：{url}：{e}")
    return None


def fetch_remote_version():
    """
    从远端获取版本号（权威源优先 + CDN 兜底，带超时与重试）

    优先返回 GitHub raw 的版本号（无缓存、始终最新），避免 CDN 缓存导致
    推送新版本后最长 12 小时内误报"已是最新版本"。
    仅当 raw 全部失败时才降级使用 jsDelivr 的值（可能陈旧，但好过检查失败）。

    Returns:
        str: 远程版本号（纯数字），全部失败返回 None
    """
    cdn_fallback = None
    for attempt in range(1, UPDATE_CHECK_RETRIES + 1):
        raw = _fetch_version_from(GITHUB_RAW_VERSION_URL)
        if raw:
            return raw
        if cdn_fallback is None:
            cdn_fallback = _fetch_version_from(GITHUB_CDN_VERSION_URL)
        if attempt < UPDATE_CHECK_RETRIES:
            time.sleep(UPDATE_CHECK_BACKOFF)
    return cdn_fallback


def check_for_updates():
    """后台任务：抓取远程版本并与本地版本对比，更新 update_check 状态"""
    remote = fetch_remote_version()
    now = datetime.now().isoformat()
    with update_check_lock:
        if remote is None:
            update_check.update({
                'status': 'error',
                'latest': None,
                'checked_at': now,
                'error': '无法连接 GitHub'
            })
            return
        update_check.update({'latest': remote, 'checked_at': now, 'error': None})
        if _parse_version_tuple(remote) > _parse_version_tuple(APP_VERSION):
            update_check['status'] = 'update-available'
        else:
            update_check['status'] = 'up-to-date'


def trigger_update_check():
    """每次页面打开时调用；若已有检查在进行则复用，否则启动后台检查"""
    with update_check_lock:
        if update_check['status'] == 'checking':
            return
        update_check['status'] = 'checking'
        update_check['error'] = None
    thread = threading.Thread(target=check_for_updates, daemon=True)
    thread.start()

# 导入 img2pdf（图片转 PDF 无损转换）
# 某些 JPEG（iPhone 拍摄带 EXIF rotation=0 异常）img2pdf 会失败时，
# 降级到 LibreOffice（convert_to_pdf）走标准转换路径。
try:
    import img2pdf
    IMG2PDF_AVAILABLE = True
except ImportError:
    IMG2PDF_AVAILABLE = False

# 导入 IPP 客户端
try:
    from ipp_client import IPPTOOL_AVAILABLE
except ImportError:
    IPPTOOL_AVAILABLE = False

# 导入打印机在线检测模块
try:
    from printer_checker import check_printer_online, IPPTOOL_AVAILABLE as CHECKER_IPPTOOL_AVAILABLE
    IPPTOOL_AVAILABLE = IPPTOOL_AVAILABLE or CHECKER_IPPTOOL_AVAILABLE
except ImportError:
    check_printer_online = None

# 导入 IPP 错误查询（用于打印后回查）
try:
    from ipp_client import get_printer_errors, get_error_guides
except ImportError:
    get_printer_errors = None
    get_error_guides = None

app = Flask(__name__)

app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['PREVIEW_FOLDER'] = os.path.join(os.path.dirname(__file__), 'previews')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB 最大文件
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'txt', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'rtf', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg'}

# 确保上传目录和预览目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PREVIEW_FOLDER'], exist_ok=True)

# 导入日志轮换处理器
from logging.handlers import RotatingFileHandler

# 配置日志（使用 RotatingFileHandler 自动轮换）
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 创建轮换处理器：单文件最大 1MB，保留 5 个备份文件
rotating_handler = RotatingFileHandler(
    filename=os.path.join(os.path.dirname(__file__), 'app.log'),
    maxBytes=1*1024*1024,     # 1MB
    backupCount=5,            # 保留 5 个备份
    encoding='utf-8'
)
rotating_handler.setLevel(logging.INFO)

# 创建控制台处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# 设置格式
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
rotating_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# 添加处理器
logger.addHandler(rotating_handler)
logger.addHandler(console_handler)

# 注册打印预览蓝图（移植自 hanxi/cups-web）
try:
    from preview_routes import preview_bp
    app.register_blueprint(preview_bp)
    logger.info("打印预览路由（preview_routes）已注册：/api/preview/*")
except ImportError as e:
    logger.warning(f"打印预览路由加载失败（不影响主功能）：{e}")

# 存储打印任务状态
print_jobs = {}
print_jobs_lock = threading.Lock()  # 添加线程锁保护共享数据

# 已终止任务状态集合：这些任务不再被 monitor 跟踪，可被自动清理
JOB_TERMINAL_STATES = {'completed', 'failed', 'timeout', 'unknown', 'error', 'cancelled'}
# 任务总数硬上限：超过时丢弃最旧的已终止任务，防止 print_jobs 无限增长
JOB_MAX_COUNT = 200


def _sanitize_job(job):
    """返回不含内部绝对路径的 job 副本，避免向客户端泄露 actual_print_file"""
    j = dict(job)
    j.pop('actual_print_file', None)
    return j


def _sweep_terminal_jobs():
    """任务总数超过上限时，丢弃最旧的已终止任务，维持 JOB_MAX_COUNT 上限。

    仅在打印提交（api_print）时触发：print_jobs 的唯一增长路径是 submit_print_job，
    因此无需在每个 /api/jobs 轮询中清退。已终止任务在终态切换时已清理 /tmp 临时文件，
    此处只移除内存中的 dict 项。
    """
    with print_jobs_lock:
        if len(print_jobs) <= JOB_MAX_COUNT:
            return
        terminal = sorted(
            ((jid, datetime.fromisoformat(j['timestamp'])) for jid, j in print_jobs.items()
             if j.get('status') in JOB_TERMINAL_STATES and j.get('timestamp')),
            key=lambda x: x[1])
        excess = len(print_jobs) - JOB_MAX_COUNT
        for jid, _ in terminal[:excess]:
            print_jobs.pop(jid, None)


# 上传取消令牌
_upload_tokens = {}
_upload_tokens_lock = threading.Lock()


def _register_upload_file(token, filepath):
    """注册上传文件路径，返回是否应终止"""
    with _upload_tokens_lock:
        entry = _upload_tokens.get(token)
        if not entry:
            return True
        entry['files'].append(filepath)
        if entry.get('cancelled'):
            return True
    return False


def _register_upload_files(token, filepaths):
    """批量注册上传文件路径，返回是否应终止"""
    with _upload_tokens_lock:
        entry = _upload_tokens.get(token)
        if not entry:
            return True
        entry['files'].extend(filepaths)
        if entry.get('cancelled'):
            return True
    return False


def _cleanup_upload(token):
    """清理 token 下所有已注册文件"""
    with _upload_tokens_lock:
        entry = _upload_tokens.pop(token, None)
    if not entry:
        return
    for path in entry.get('files', []):
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"已清理取消上传的残留文件：{path}")
        except Exception as e:
            logger.warning(f"清理残留文件失败：{path}, {e}")

def _lpstat(args, timeout=5):
    """执行 lpstat 并强制英文输出，避免 locale 影响解析"""
    return subprocess.run(
        ['lpstat'] + args,
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, 'LC_ALL': 'C'}
    )


def is_safe_path(base_path, target_path):
    """
    检查目标路径是否在基础路径内，防止路径遍历攻击

    Args:
        base_path: 允许访问的基础目录
        target_path: 目标路径

    Returns:
        bool: 安全返回True，不安全返回False
    """
    # 规范化路径，解析所有符号链接和相对路径
    base_abs = os.path.abspath(base_path)
    target_abs = os.path.abspath(target_path)

    # 检查目标路径是否以基础路径开头
    return target_abs.startswith(base_abs + os.sep) or target_abs == base_abs


def _recommend_ipp_queue(current_printer_name):
    """
    检查当前队列是否是 IPP 队列，如果不是，推荐一个 IPP 队列
    用于提示用户切换到更稳定的 IPP 队列（ipp-usb）

    Args:
        current_printer_name: 当前选中的打印机

    Returns:
        dict | None: {'preferred': '队列名', 'reason': '...', 'message': '...'} 或 None
    """
    try:
        current_uri = get_printer_uri(current_printer_name)
        if not current_uri:
            return None

        # 已经是 IPP 队列，无需推荐
        is_current_ipp = current_uri.startswith(('ipp://', 'ipps://', 'dnssd://', 'http://', 'https://'))
        if is_current_ipp:
            return None

        # 找一个 IPP 队列
        printers = get_printers_fast()
        for p in printers:
            uri = p.get('uri', '') or ''
            if uri.startswith(('ipp://', 'ipps://', 'dnssd://')):
                reason = 'cnijbe2 后端长时间空闲后容易卡死且无法自检错误'
                if uri.startswith('dnssd://'):
                    reason = 'ipp-usb 标准协议（推荐），能自动检测卡纸/缺纸/墨水等错误'
                elif uri.startswith(('ipp://', 'ipps://')):
                    reason = 'IPP 标准协议（推荐），能自动检测卡纸/缺纸/墨水等错误'
                return {
                    'preferred': p['name'],
                    'reason': reason,
                    'message': f"当前队列 {current_printer_name} 是 {current_uri.split('://')[0]} 私有协议，无法检测卡纸/缺纸/墨盒错误，建议切换到 IPP 队列 {p['name']}",
                }
        return None
    except Exception as e:
        logger.debug(f"IPP 队列推荐检查失败: {e}")
        return None


def _check_printer_after_job(printer_name, timeout=5):
    """
    打印任务"完成"后回查打印机真实状态
    用于发现 cnijbe2 假成功、ipp 假成功等情况

    优先级：
    1. IPP 状态（适用 IPP 队列）
    2. lpstat 队列历史 Alerts 字段（适用私有协议队列，如 cnijbe2）
       关键 Alerts: 'job-completed' / 'job-canceled-by-user' / 'job-aborted-by-system'

    Args:
        printer_name: 打印机 CUPS 名称
        timeout: IPP 查询超时（秒）

    Returns:
        dict: {
            'available': bool,
            'has_error': bool,
            'reasons': list,       # 原始 reason 列表
            'reasons_cn': list,    # 中文翻译
            'printer_state': str|None,
            'printer_alert_description': str|None,
            'error': str|None,
            'source': 'ipp' | 'lpstat' | None,
        }
    """
    if get_printer_errors is None:
        # ipp 不可用时，至少用 lpstat 兜底
        return _check_via_lpstat_history(printer_name)

    try:
        # 1. 取打印机 URI
        uri = get_printer_uri(printer_name)
        if not uri:
            return _check_via_lpstat_history(printer_name)

        # 2. 解析 URI，决定走 IPP 还是 lpstat 兜底
        if uri.startswith('dnssd://'):
            resolved = _resolve_dnssd_uri(uri, timeout=3)
            target_uri = resolved or uri
        elif uri.startswith(('ipp://', 'ipps://', 'http://', 'https://')):
            target_uri = uri
        else:
            # usb/cnijbe2/socket/bjnp 等私有协议，走 lpstat 兜底
            return _check_via_lpstat_history(printer_name)

        # 3. IPP 查状态
        result = get_printer_errors(target_uri, timeout=timeout)
        if not result.get('available'):
            # IPP 失败时回退到 lpstat
            fallback = _check_via_lpstat_history(printer_name)
            if fallback:
                fallback['error'] = result.get('error')
                fallback['source'] = 'lpstat (IPP 失败后兜底)'
            return fallback

        # 4. 判断是否有错误
        reasons = result.get('printer_state_reasons', []) or []
        state = result.get('printer_state')
        is_normal = (reasons == ['none'] or reasons == [])
        has_error = (not is_normal) or (state == 'stopped')

        # 5. 翻译成中文
        reasons_cn = []
        if get_error_guides:
            for g in get_error_guides(reasons):
                reasons_cn.append(g.get('cn', g.get('reason')))

        return {
            'available': True,
            'has_error': has_error,
            'reasons': reasons,
            'reasons_cn': reasons_cn,
            'printer_state': state,
            'printer_alert_description': result.get('printer_alert_description'),
            'error': None,
            'source': 'ipp',
        }
    except Exception as e:
        logger.warning(f"打印后回查打印机状态失败：{printer_name}, {e}")
        return _check_via_lpstat_history(printer_name)


def _check_via_lpstat_history(printer_name):
    """
    通过 lpstat 历史队列检测 cnijbe2 等私有协议队列的打印异常
    当一个打印任务很快完成（<5秒）但实际并没有数据送到打印机，
    CUPS 历史里的 Alerts 字段会是 'job-aborted-by-system' 之类

    Returns:
        dict 或 None
    """
    try:
        result = _lpstat(['-l', '-W', 'all', '-o', printer_name])
        if result.returncode != 0:
            return None

        # 取最近 3 个任务，看是否有警告
        recent_jobs = []
        current = None
        for line in result.stdout.split('\n'):
            # 新任务行: "打印机名-N  user  size  date"
            job_match = re.match(rf'{re.escape(printer_name)}-(\d+)\s+(\S+)\s+(\d+)\s+(.*)', line.strip())
            if job_match:
                if current:
                    recent_jobs.append(current)
                current = {
                    'job_id': job_match.group(1),
                    'user': job_match.group(2),
                    'alerts': '',
                }
            elif current and 'Alerts:' in line:
                current['alerts'] = line.split('Alerts:', 1)[1].strip()
        if current:
            recent_jobs.append(current)

        if not recent_jobs:
            return None

        # 看最近 1 个任务
        last = recent_jobs[0]
        alerts = last.get('alerts', '')

        # 正常的完成（与 get_job_final_state 保持一致的判断逻辑）：
        #   - 空 / 'none' / 'job-completed' / 'processing-to-stop-point' / 'none-warnings' / 'report-on-success'
        # 失败：
        #   - 'job-canceled-by-user' / 'job-aborted-by-system' / 'job-halted-while-pending' / 'aborted-by-system'
        normal_alerts = ('', 'none', 'job-completed', 'processing-to-stop-point',
                          'none-warnings', 'report-on-success')
        if alerts in normal_alerts:
            return {
                'available': True,
                'has_error': False,
                'reasons': [],
                'reasons_cn': [],
                'printer_state': None,
                'printer_alert_description': None,
                'error': None,
                'source': 'lpstat',
            }

        # 有错误
        reason_map = {
            'job-canceled-by-user': 'job-canceled',
            'job-aborted-by-system': 'job-aborted',
            'job-halted-while-pending': 'job-halted',
        }
        reason = reason_map.get(alerts, alerts)
        cn_map = {
            'job-canceled': '打印任务被取消',
            'job-aborted': '打印任务被系统中止（可能打印机通信失败）',
            'job-halted': '打印任务挂起',
        }
        cn = cn_map.get(reason, f'CUPS 报告：{alerts}')

        return {
            'available': True,
            'has_error': True,
            'reasons': [reason],
            'reasons_cn': [cn],
            'printer_state': None,
            'printer_alert_description': f'CUPS Alerts: {alerts}',
            'error': None,
            'source': 'lpstat',
        }
    except Exception as e:
        logger.debug(f"lpstat 历史回查失败：{e}")
        return None


def _resolve_dnssd_uri(dnssd_uri, timeout=3):
    """
    将 dnssd:// 服务名._ipp._tcp.local/?uuid=... 解析为实际可达的 ipp://ip:port/ipp/print

    ipp-usb 发布的队列走 dnssd:// 形式，CUPS 默认的 lpinfo 看到的就是这种 URI。
    但 ipptool 不接受 dnssd://，需要先用 avahi-browse/ippfind 解析成 IP:port。

    Args:
        dnssd_uri: 形如 dnssd://CanonTS3380._ipp._tcp.local/?uuid=00000000-0000-1000-8000-0018a294893c
        timeout: 解析超时（秒）

    Returns:
        可直接给 ipptool 用的 URI（ipp://127.0.0.1:60001/ipp/print），失败返回 None
    """
    try:
        # 从 URI 中提取 service-instance-name（dnssd:// 后到第一个 / 或 ? 之前）
        # 例：dnssd://CanonTS3380._ipp._tcp.local/?uuid=... → CanonTS3380._ipp._tcp.local
        from urllib.parse import urlparse
        parsed = urlparse(dnssd_uri)
        service_name = parsed.hostname
        if not service_name:
            return None

        # 取短名（ippfind 用短名匹配）: CanonTS3380._ipp._tcp.local → CanonTS3380
        short_name = service_name.split('.')[0] if service_name else ''

        # 优先用 ippfind（更标准，ipp-usb 部署都有）
        # 语法: ippfind [-T timeout] [reg-type] [service-name-pattern]
        try:
            r = subprocess.run(
                ['ippfind', '-T', str(timeout), '_ipp._tcp', short_name],
                capture_output=True, text=True, timeout=timeout + 2
            )
            if r.returncode == 0 and r.stdout.strip():
                resolved = r.stdout.strip().split('\n')[0]
                logger.debug(f"ippfind 解析 {service_name} -> {resolved}")
                return resolved
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.debug(f"ippfind 不可用: {e}")

        # 回退：avahi-browse（一般发行版都有）
        try:
            # -t: 一次性查询，-r: 解析，-k: 忽略本地缓存
            r = subprocess.run(
                ['avahi-browse', '-tr', '_ipp._tcp'],
                capture_output=True, text=True, timeout=timeout + 2
            )
            for line in r.stdout.split('\n'):
                # 格式: + wlan0 IPv4 CanonTS3380 Internet Printer local
                if short_name in line:
                    # 用 avahi-resolve 解析主机名
                    host_r = subprocess.run(
                        ['avahi-resolve', '-n', f'{short_name}.local'],
                        capture_output=True, text=True, timeout=2
                    )
                    if host_r.returncode == 0:
                        # 输出: hostname    192.168.1.x
                        parts = host_r.stdout.strip().split()
                        if len(parts) >= 2:
                            return f"ipp://{parts[1]}:631/ipp/print"
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.debug(f"avahi-browse 不可用: {e}")

        return None
    except Exception as e:
        logger.warning(f"dnssd 解析失败: {dnssd_uri}, {e}")
        return None


def get_printer_uri(printer_name):
    """
    获取打印机URI

    Args:
        printer_name: 打印机名称

    Returns:
        打印机URI字符串，如果失败返回None
    """
    try:
        result = _lpstat(['-p', printer_name, '-v'])

        if result.returncode != 0:
            logger.error(f"获取打印机URI失败: {result.stderr}")
            return None

        printer_uri = None
        for line in result.stdout.split('\n'):
            match = re.search(r'device\s+for\s+' + re.escape(printer_name) + r':\s*(\S+)', line)
            if match:
                printer_uri = match.group(1).strip()
                break

        return printer_uri

    except subprocess.TimeoutExpired:
        logger.error(f"获取打印机URI超时")
        return None
    except Exception as e:
        logger.error(f"获取打印机URI失败: {e}")
        return None


# ---- 打印机 URI 自愈：ipp-usb 虚拟端口漂移自动修复 ----
# ipp-usb 把 USB 打印机映射为 127.0.0.1:60000 起的虚拟 IPP 端点；
# 打印机 USB 重枚举（断电重启/拔插/卡纸处理）后端口可能漂移（如 60001→60000），
# 导致 CUPS 队列 URI 失效、监控显示"离线"/"Host is down"。
# 这里在探测失败时自动扫描候选端口，找到同型号打印机后 lpadmin 修正队列 URI。

_heal_lock = threading.Lock()


def _get_printer_description(printer_name):
    """从 lpstat -l 取队列 Description（如 Canon TS3380 (IPP-over-USB)），用于识别打印机型号。"""
    try:
        result = _lpstat(['-l', '-p', printer_name])
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if line.strip().lower().startswith('description:'):
                    return line.split(':', 1)[1].strip()
    except Exception as e:
        logger.debug(f"读取打印机描述失败 {printer_name}: {e}")
    return ''


def _probe_ipp_endpoint(uri, timeout=3):
    """用 ipptool 探测候选 IPP 端点，成功返回 {'make_model','printer_name'}，失败返回 None。"""
    if not IPPTOOL_AVAILABLE:
        return None
    test_content = (
        '{\n'
        '    NAME "Heal-Probe"\n'
        '    OPERATION Get-Printer-Attributes\n'
        '    GROUP operation\n'
        '    ATTR charset attributes-charset utf-8\n'
        '    ATTR language attributes-natural-language en\n'
        '    ATTR uri printer-uri ' + uri + '\n'
        '    ATTR keyword requested-attributes printer-state,make-and-model,printer-name\n'
        '}\n'
    )
    fd, test_file = tempfile.mkstemp(suffix='.test')
    try:
        os.write(fd, test_content.encode('utf-8'))
        os.close(fd)
        result = subprocess.run(
            ['ipptool', '-tv', '-T', str(timeout), uri, test_file],
            capture_output=True, text=True, timeout=timeout + 3)
        if '[PASS]' in result.stdout:
            mm = re.search(r'make-and-model.*?=\s*(.+)', result.stdout)
            pn = re.search(r'printer-name.*?=\s*(.+)', result.stdout)
            return {
                'make_model': mm.group(1).strip() if mm else '',
                'printer_name': pn.group(1).strip() if pn else '',
            }
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"探测 {uri} 失败: {e}")
    except Exception as e:
        logger.debug(f"探测 {uri} 异常: {e}")
    finally:
        try:
            os.unlink(test_file)
        except OSError:
            pass
    return None


def auto_heal_ipp_usb_uri(printer_name, current_uri, timeout=3):
    """
    ipp-usb 虚拟端口漂移自愈。

    仅当当前队列 URI 是 127.0.0.1/localhost 的 ipp:// 端点且探测失败时调用：
    扫描 60000-60010，找到型号匹配的 IPP 打印机端点，并用 lpadmin 修正 CUPS 队列 URI。

    Args:
        printer_name: CUPS 队列名
        current_uri:  当前队列 URI（可能已失效）
        timeout:      单端口探测超时（秒）

    Returns:
        bool: 是否修复成功
    """
    if not current_uri:
        return False
    try:
        parsed = urlparse(current_uri)
    except Exception:
        return False
    if parsed.scheme not in ('ipp', 'ipps') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        return False
    if not IPPTOOL_AVAILABLE:
        logger.warning(f"打印机自愈跳过（ipptool 不可用）：{printer_name}")
        return False

    # 期望识别令牌：从队列描述与队列名提取品牌/型号（如 canon / ts3380）
    brands = ('canon', 'hp', 'epson', 'brother', 'pixma', 'lexmark', 'xerox', 'samsung', 'ricoh')
    raw_tokens = re.findall(r'[a-z0-9]+', _get_printer_description(printer_name).lower()) \
        + re.findall(r'[a-z0-9]+', printer_name.lower())
    expected = []
    for t in raw_tokens:
        if len(t) < 3 or t in expected:
            continue
        if t in brands or re.search(r'\d', t):
            expected.append(t)
    if not expected:
        expected = [t for t in raw_tokens if len(t) >= 3][:4]  # 兜底：任意非短令牌
    logger.info(f"打印机自愈扫描：queue={printer_name}, uri={current_uri}, 识别令牌={expected or ['任意IPP端点']}")

    with _heal_lock:
        # 1. 收集所有存活的 IPP 端点（ipptool Get-Printer-Attributes 通过者）
        live = []  # [(uri, info)]
        for port in range(60000, 60011):
            candidate = f'ipp://127.0.0.1:{port}/ipp/print'
            if candidate == current_uri:
                continue
            info = _probe_ipp_endpoint(candidate, timeout=timeout)
            if info:
                live.append((candidate, info))

        if not live:
            logger.warning(f"打印机自愈：60000-60010 范围内未发现任何存活 IPP 端点，无法自愈")
            return False

        # 2. 选定目标：
        #    - 仅一个存活端点 → 就是同一台打印机（ipp-usb 只为真实 USB 打印机开端点）
        #    - 多个存活端点 → 用型号令牌甄别，匹配不上则放弃，避免误绑定
        chosen = None
        if len(live) == 1:
            chosen = live[0][0]
            logger.info(f"打印机自愈：范围内仅 {chosen} 一个存活端点，判定为同一台打印机")
        else:
            for candidate, info in live:
                hay = (info.get('make_model', '') + ' ' + info.get('printer_name', '')).lower()
                if expected and any(tok in hay for tok in expected if tok):
                    chosen = candidate
                    logger.info(f"打印机自愈：端点 {candidate} 型号匹配（{hay.strip() or '无型号信息'}）")
                    break
            if chosen is None:
                logger.warning(f"打印机自愈：存在 {len(live)} 个存活端点且型号无法匹配，跳过以避免误绑定")
                return False

        # 3. lpadmin 修正 CUPS 队列 URI
        logger.warning(f"打印机自愈：发现新端点 {chosen}（原 {current_uri}），正在修正 CUPS 队列...")
        try:
            res = subprocess.run(
                ['lpadmin', '-p', printer_name, '-v', chosen],
                capture_output=True, text=True, timeout=timeout + 5)
            if res.returncode == 0:
                logger.warning(f"打印机自愈成功：{printer_name} {current_uri} -> {chosen}")
                return True
            logger.error(f"打印机自愈失败（lpadmin）：{res.stderr.strip()}")
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.error(f"打印机自愈失败（lpadmin 异常）：{e}")
        return False




def get_single_printer_status(printer_name, timeout=5):
    """
    获取单台打印机的在线状态（只探测这一台，不探测其他打印机）

    Args:
        printer_name: 打印机名称
        timeout: 超时时间（秒），默认 5 秒

    Returns:
        dict: {
            'name': str,
            'status': str,
            'online_status': str,
            'uri': str or None
        }
    """
    try:
        # 1. 获取打印机 URI
        printer_uri = get_printer_uri(printer_name)
        
        if not printer_uri:
            # 打印机不存在或获取 URI 失败，返回 None 以便上层返回 404
            return None
        
        # 2. 获取 CUPS 队列状态
        status = 'idle'
        try:
            result = _lpstat(['-p', printer_name])
            if result.returncode == 0:
                line = result.stdout.strip()
                if 'is ready' in line.lower():
                    status = 'ready'
                elif 'is processing' in line.lower():
                    status = 'processing'
                elif 'is stopped' in line.lower():
                    status = 'stopped'
        except Exception as e:
            logger.warning(f"获取打印机 {printer_name} 队列状态失败：{e}")
        
        # 3. 使用协议级探测检查打印机是否真实在线（只探测这一台）
        online_status = 'unknown'
        if check_printer_online:
            try:
                probe_result = check_printer_online(printer_uri, timeout=timeout)
                if probe_result.get('online'):
                    online_status = 'online'
                else:
                    online_status = 'offline'
                    # 如果探测离线，更新状态显示
                    if status == 'idle':
                        status = 'offline'
                    # ★ 自愈：ipp-usb 虚拟端口漂移时自动扫描并修正 CUPS 队列 URI，然后复测
                    healed = auto_heal_ipp_usb_uri(printer_name, printer_uri, timeout=timeout)
                    if healed:
                        printer_uri = get_printer_uri(printer_name)
                        probe_result = check_printer_online(printer_uri, timeout=timeout)
                        if probe_result.get('online'):
                            online_status = 'online'
                            status = 'ready'
                            logger.warning(f"打印机 {printer_name} 自愈后恢复在线：{printer_uri}")
                        else:
                            logger.warning(f"打印机 {printer_name} 已修正 URI 但仍探测失败：{printer_uri} -> {probe_result}")
                logger.debug(f"打印机 {printer_name} 在线检测：{probe_result}")
            except Exception as e:
                logger.warning(f"打印机 {printer_name} 在线检测失败：{e}")
                online_status = 'unknown'
        
        return {
            'name': printer_name,
            'status': status,
            'online_status': online_status,
            'uri': printer_uri
        }
        
    except Exception as e:
        logger.error(f"获取单台打印机状态失败：{printer_name}, 错误：{e}")
        return {
            'name': printer_name,
            'status': 'unknown',
            'online_status': 'unknown',
            'uri': None
        }

def get_safe_path(base_path, filename):
    """
    获取安全的文件路径，防止路径遍历攻击

    Args:
        base_path: 允许访问的基础目录
        filename: 文件名

    Returns:
        str: 安全的文件路径，如果不安全返回None
    """
    # 移除所有路径遍历字符
    filename = os.path.basename(filename)

    # 拼接完整路径
    filepath = os.path.join(base_path, filename)

    # 检查路径安全性
    if is_safe_path(base_path, filepath):
        return filepath
    else:
        logger.warning(f"检测到潜在的路径遍历攻击: {filename}")
        return None


def safe_filename(filename, allowed_extensions):
    """
    自定义安全文件名处理，保留中文等非ASCII字符

    Args:
        filename: 原始文件名
        allowed_extensions: 允许的扩展名集合

    Returns:
        str: 安全的文件名；若文件名无效或扩展名不被允许则返回 None
    """
    # 1. 移除路径部分，只保留文件名
    filename = os.path.basename(filename)

    # 2. 空文件名直接拒绝
    if not filename:
        return None

    # 3. 提取并验证扩展名
    name_part, ext = os.path.splitext(filename)
    ext = ext.lower()

    # 如果无扩展名或扩展名不在允许列表中，直接拒绝
    if not ext or ext.lstrip('.') not in allowed_extensions:
        return None

    # 4. 清理文件名中的非法字符（保留中文、英文、数字、下划线、连字符、空格、括号等）
    # 移除路径分隔符和控制字符
    illegal_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '[', ']', ';', '\x00']
    safe_name = name_part
    for char in illegal_chars:
        safe_name = safe_name.replace(char, '')

    # 移除 '..' 防止路径遍历
    safe_name = safe_name.replace('..', '')

    # 5. 如果文件名为空，直接拒绝
    if not safe_name.strip():
        return None

    # 6. 限制文件名长度（避免文件系统限制）
    if len(safe_name) > 200:
        safe_name = safe_name[:200]

    return f"{safe_name}{ext}"


def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def is_image_file(filename):
    """检查是否为图片文件"""
    image_extensions = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in image_extensions

def is_document_file(filename):
    """检查是否为文档文件（非PDF）"""
    doc_extensions = {'txt', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'rtf'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in doc_extensions



def convert_pdf_to_images(pdf_path, output_dir, dpi=150, pdf_filename=None):
    """
    Convert PDF pages to PNG images using pdftoppm

    Args:
        pdf_path: Input PDF file path
        output_dir: Output directory
        dpi: Resolution in dots per inch (default 150)
        pdf_filename: PDF filename (e.g., "document.pdf")

    Returns:
        List of generated image paths, or empty list if failed
    """
    try:
        # Check if pdftoppm is available
        try:
            subprocess.run(['pdftoppm', '-h'], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.error("pdftoppm not available (install poppler-utils)")
            return []

        # Generate output filename prefix (same as PDF name without .pdf extension)
        # Use provided filename (keeps timestamp) if available
        if pdf_filename:
            # Remove .pdf extension if present
            if pdf_filename.lower().endswith('.pdf'):
                pdf_name = os.path.splitext(pdf_filename)[0]
            else:
                pdf_name = pdf_filename
        else:
            # Extract from pdf_path
            pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
        
        output_prefix = os.path.join(output_dir, pdf_name)

        # Convert PDF to PNG images
        # -png: output format
        # -r: resolution in DPI
        # Output files will be: {prefix}-1.png, {prefix}-2.png, ...
        cmd = ['pdftoppm', '-png', '-r', str(dpi), pdf_path, output_prefix]

        logger.info(f"Converting PDF to images: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            # Find all generated images using glob
            # pdftoppm generates files like: prefix-1.png, prefix-2.png, ...
            image_pattern = f"{glob.escape(output_prefix)}-*.png"
            image_files = sorted(glob.glob(image_pattern))

            logger.info(f"Generated {len(image_files)} page images")
            return image_files

        logger.error(f"PDF to image conversion failed: {result.stderr}")
        return []

    except subprocess.TimeoutExpired:
        logger.error("PDF to image conversion timeout")
        return []
    except Exception as e:
        logger.error(f"PDF to image conversion failed: {e}")
        return []



def get_preview_images(pdf_filename):
    """
    Get list of preview images for a PDF file
    
    Args:
        pdf_filename: PDF filename (e.g., "document.pdf")
    
    Returns:
        List of dicts with page number and image path
    """
    try:
        
        # Get the base name without extension
        if pdf_filename.lower().endswith('.pdf'):
            base_name = pdf_filename[:-4]
        else:
            base_name = pdf_filename
        
        # Image files are named: {base_name}-1.png, {base_name}-2.png, ...
        image_pattern = os.path.join(app.config['PREVIEW_FOLDER'], f"{glob.escape(base_name)}-*.png")
        image_files = sorted(glob.glob(image_pattern))
        
        # Extract page numbers from filenames
        images = []
        for img_path in image_files:
            img_filename = os.path.basename(img_path)
            # Extract page number from filename like "document-1.png"
            m = re.search(r'-(\d+)\.png$', img_filename)
            if m:
                page_num = int(m.group(1))
                images.append({
                    'page': page_num,
                    'filename': img_filename
                    # Removed 'path' for security - frontend uses /api/preview/ endpoint
                })
        
        return images
    
    except Exception as e:
        logger.error(f"Failed to get preview images: {e}")
        return []


def convert_to_pdf(input_file, output_dir):
    """
    使用 LibreOffice 将文档转换为 PDF

    Args:
        input_file: 输入文件路径
        output_dir: 输出目录

    Returns:
        转换后的 PDF 文件路径，如果失败返回 None
    """
    try:
        filename = os.path.basename(input_file)
        name, ext = os.path.splitext(filename)

        # 检查 libreoffice 是否可用
        try:
            subprocess.run(['libreoffice', '--version'],
                         capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.error("LibreOffice 未安装或不可用")
            return None

        # soffice headless 启动时需要 UserInstallation 目录存放用户配置。
        # 在 fnOS 应用隔离下，cups-web-print 用户没有 /run/user/<uid>/ 写权限，
        # 默认会触发 "User installation could not be completed" 致命错误。
        # 显式指定一个当前用户可写的目录（/tmp 下，仅当前进程有效）。
        user_profile_dir = '/tmp/cups_web_lo_profile'
        try:
            os.makedirs(user_profile_dir, mode=0o755, exist_ok=True)
        except OSError as e:
            logger.warning(f"无法创建 LibreOffice 用户配置目录 {user_profile_dir}: {e}")

        # 使用 libreoffice 转换
        cmd = [
            'libreoffice',
            '--headless',
            f'-env:UserInstallation=file://{user_profile_dir}',
            '--convert-to', 'pdf',
            '--outdir', output_dir,
            input_file
        ]

        logger.info(f"开始转换文档：{filename}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # 检查转换后的 PDF 文件是否存在（LibreOffice 可能返回非零退出码但实际转换成功）
        pdf_filename = f"{name}.pdf"
        pdf_path = os.path.join(output_dir, pdf_filename)

        if os.path.exists(pdf_path):
            logger.info(f"文档转换成功：{pdf_path}")
            # 注意：图片预览由 api_upload() 统一生成，避免重复调用
            return pdf_path
        else:
            # 转换失败，记录详细错误
            error_msg = result.stderr.strip() if result.stderr else "未知错误"
            logger.error(f"文档转换失败：{error_msg}")
            logger.error(f"PDF 文件不存在：{pdf_path}")
            return None

    except subprocess.TimeoutExpired:
        logger.error("文档转换超时")
        return None
    except Exception as e:
        logger.error(f"文档转换异常：{e}")
        return None

def convert_image_to_pdf(input_file, output_dir):
    """
    将图片转换为 PDF
    - raster 图片（jpg/jpeg/png/gif/bmp）：使用 img2pdf（无损），不可用时降级 LibreOffice
    - SVG：使用 LibreOffice 转换

    Args:
        input_file: 输入图片路径
        output_dir: 输出目录

    Returns:
        转换后的 PDF 文件路径，如果失败返回 None
    """
    filename = os.path.basename(input_file)
    name, ext = os.path.splitext(filename)
    ext_lower = ext.lower()
    pdf_filename = f"{name}.pdf"
    pdf_path = os.path.join(output_dir, pdf_filename)

    # SVG：img2pdf 不支持，直接用 LibreOffice
    if ext_lower == '.svg':
        logger.info(f"SVG 文件使用 LibreOffice 转换：{filename}")
        return convert_to_pdf(input_file, output_dir)

    # raster 图片：优先使用 img2pdf（无损）
    if IMG2PDF_AVAILABLE:
        try:
            with open(input_file, 'rb') as f:
                pdf_bytes = img2pdf.convert(f.read())
            with open(pdf_path, 'wb') as f:
                f.write(pdf_bytes)
            if os.path.exists(pdf_path):
                logger.info(f"图片转换成功（img2pdf）：{pdf_path}")
                return pdf_path
            logger.error(f"图片转换失败（img2pdf），输出不存在：{pdf_path}")
        except Exception as e:
            logger.warning(f"img2pdf 转换失败（{e}），降级到 LibreOffice")

    # fallback：使用 LibreOffice
    logger.info(f"使用 LibreOffice 转换图片：{filename}")
    return convert_to_pdf(input_file, output_dir)

def get_preview_file(original_filename):
    """
    获取预览文件路径

    Args:
        original_filename: 原始文件名

    Returns:
        预览文件路径（PDF），如果无法预览返回 None

    注意：PDF 复制和文档转换应在 api_upload() 中完成，这里只返回已存在的路径
    """
    # 安全检查：只获取文件名，移除路径部分
    original_filename = os.path.basename(original_filename)

    # 统一逻辑：所有文件类型都返回 previews/目录中对应的 PDF
    # 如果是 PDF，直接返回 previews/目录的 PDF
    if original_filename.lower().endswith('.pdf'):
        pdf_path = get_safe_path(app.config['PREVIEW_FOLDER'], original_filename)
        if pdf_path and os.path.exists(pdf_path):
            return pdf_path
        return None

    # 图片和文档：返回 previews/目录的 {basename}.pdf
    if is_image_file(original_filename) or is_document_file(original_filename):
        pdf_filename = os.path.splitext(original_filename)[0] + '.pdf'
        pdf_path = get_safe_path(app.config['PREVIEW_FOLDER'], pdf_filename)
        if pdf_path and os.path.exists(pdf_path):
            return pdf_path
        return None

    return None



def get_printers_fast():
    """获取可用的 CUPS 打印机列表（快速版本，不进行在线探测）"""
    try:
        result = _lpstat(['-p'])
        printers = []
        if result.returncode == 0:
            lines_output = result.stdout.strip().split('\n')
            for line in lines_output:
                # 检测包含"printer"关键字的行
                if 'printer' in line.lower():
                    # 提取打印机名称
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].lower() == 'printer':
                        printer_name = parts[1]
                        # 提取状态
                        status = 'idle'
                        if 'is ready' in line.lower():
                            status = 'ready'
                        elif 'is processing' in line.lower():
                            status = 'processing'
                        elif 'is stopped' in line.lower():
                            status = 'stopped'

                        # 获取打印机 URI
                        printer_uri = get_printer_uri(printer_name)

                        # 快速版本：不进行在线探测，初始状态为 unknown
                        printers.append({
                            'name': printer_name,
                            'status': status,
                            'uri': printer_uri,
                            'online_status': 'unknown'
                        })

        if not printers:
            logger.warning("未检测到可用打印机")

        return printers
    except Exception as e:
        logger.error(f"获取打印机列表失败：{e}")
        return []





def extract_pdf_pages_to_tmp(input_pdf, page_range):
    """
    使用 pdftk 提取 PDF 指定页面到 /tmp 目录（系统重启后自动清除）

    Args:
        input_pdf: 输入 PDF 路径
        page_range: 页面范围，如 "1-5 8 10-12"（空格分隔，不支持逗号）

    Returns:
        (pdf_path, error_message) 元组
    """
    try:
        # 生成输出文件名到/tmp 目录
        base_name = os.path.splitext(os.path.basename(input_pdf))[0]
        unique_id = uuid.uuid4().hex[:8]
        output_pdf = os.path.join('/tmp', f"print_{base_name}_{unique_id}_pages_{page_range.replace('-', '_').replace(',', '_').replace(' ', '_')}.pdf")

        # 检查 pdftk 是否可用
        try:
            result = subprocess.run(['pdftk', '--version'], capture_output=True, text=True, timeout=5)
            logger.info(f"pdftk 版本：{result.stdout.strip()[:100] if result.stdout else 'available'}")
        except FileNotFoundError:
            logger.error("pdftk 未安装")
            return None, "pdftk 未安装，无法提取页面"
        except subprocess.TimeoutExpired:
            return None, "pdftk 响应超时"

        # 使用 pdftk 提取页面到/tmp
        # pdftk 需要空格分隔的参数，如：pdftk input.pdf cat 2 4 output out.pdf
        # 将 page_range 按空格分割成多个参数
        page_parts = page_range.split()
        cmd = ['pdftk', input_pdf, 'cat'] + page_parts + ['output', output_pdf]
        logger.info(f"提取 PDF 页面到/tmp: {cmd}")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0 and os.path.exists(output_pdf):
            logger.info(f"PDF 页面提取成功：{output_pdf}")
            return output_pdf, None

        error_msg = result.stderr.strip() if result.stderr else "未知错误"
        logger.error(f"PDF 页面提取失败：{error_msg}")
        return None, f"PDF 页面提取失败：{error_msg}"

    except subprocess.TimeoutExpired:
        logger.error("PDF 页面提取超时")
        return None, "PDF 页面提取超时"
    except Exception as e:
        logger.error(f"PDF 页面提取异常：{e}")
        return None, f"PDF 页面提取异常：{str(e)}"


def get_printable_file(filepath, filename, page_range=None):
    """
    获取可打印的文件路径
    - PDF 和图片：返回 previews/或 uploads/目录的文件路径
    - Office 文档：返回 previews/目录的预览 PDF（如不存在则返回错误）
    - 如指定页面范围，生成对应页面的临时 PDF 到/tmp 目录（系统重启后自动清除）

    注意：PDF 复制和文档转换应在 api_upload() 中完成，如文件不存在请重新上传

    Args:
        filepath: 原始文件路径
        filename: 文件名
        page_range: 页面范围，如 "1-5 8 10-12"（空格分隔，不支持逗号）

    Returns:
        (printable_path, error_message, is_temp_file) 元组
        成功返回 (文件路径，None, 是否临时文件)，失败返回 (None, 错误信息，False)
    """
    # 所有文件类型统一路径：在 previews/ 目录找 {base_name}.pdf
    base_name = os.path.splitext(filename)[0]
    pdf_filename = f"{base_name}.pdf"
    pdf_path = get_safe_path(app.config['PREVIEW_FOLDER'], pdf_filename)

    if not pdf_path:
        logger.error(f"预览文件路径不安全：{pdf_filename}")
        return None, f"预览文件路径错误：{pdf_filename}", False
    if not os.path.exists(pdf_path):
        logger.error(f"预览 PDF 不存在：{pdf_filename}，请重新上传")
        return None, f"预览文件不存在：{pdf_filename}，请重新上传", False

    logger.info(f"使用预览 PDF: {pdf_path}")
    if page_range and page_range.strip():
        logger.info(f"从预览 PDF 提取页面范围：{page_range}")
        extracted_pdf, error = extract_pdf_pages_to_tmp(pdf_path, page_range.strip())
        if extracted_pdf:
            return extracted_pdf, None, True
        return None, error, False
    return pdf_path, None, False


def submit_print_job(filepath, printer_name, color_mode='mono', duplex='one-sided', orientation='portrait', paper_size='A4', paper_type='plain', copies=1, page_range=None, mirror=False, print_scaling='fit'):
    """
    提交打印任务到CUPS

    Args:
        filepath: 文件路径
        printer_name: 打印机名称
        color_mode: color/mono
        duplex: one-sided/two-sided-long-edge/two-sided-short-edge
        orientation: portrait/landscape (打印方向)
        paper_size: 纸张大小 (A4, A3, A5, 3.5x5, 4x6, 5x7, 8x10)
        paper_type: 纸张材质 (plain, photo, glossy, matte, envelope, cardstock, labels, auto)
        copies: 打印份数
        page_range: 页面范围，格式如 "1-5 8 10-12"（空格分隔，不支持逗号）
        mirror: 是否镜像打印
        print_scaling: 打印缩放 (none, fill, fit, auto-fit, auto)
    """
    job_id = str(uuid.uuid4())
    filename = os.path.basename(filepath)
    try:
        # 所有文件类型（文档、图片、PDF）均有对应的预览 PDF
        actual_print_file, conversion_error, _ = get_printable_file(filepath, filename, page_range)
        
        if conversion_error:
            logger.error(f"文件转换失败：{conversion_error}")
            with print_jobs_lock:
                print_jobs[job_id] = {
                    'id': job_id,
                    'filename': filename,
                    'printer': printer_name,
                    'status': 'failed',
                    'message': conversion_error,
                    'timestamp': datetime.now().isoformat(),
                    'progress': 0
                }
            return job_id, False
        
        # 构建lp命令
        cmd = ['lp', '-d', printer_name, '-n', str(copies)]

        # 添加纸张大小设置
        # PWG 5101.1 标准纸张大小映射（多品牌打印机兼容）
        # 参考：https://www.pwg.org/standards.html
        # 格式说明：iso_* = ISO 标准，na_* = 北美标准，jpn_* = 日本标准，om_* = 其他标准
        paper_size_map = {
            # ISO 标准纸张 (A 系列)
            'A4': 'iso_a4_210x297mm',    # A4 (210×297mm)
            'A3': 'iso_a3_297x420mm',    # A3 (297×420mm)
            'A2': 'iso_a2_420x594mm',    # A2 (420×594mm)
            'A1': 'iso_a1_594x841mm',    # A1 (594×841mm)
            'A5': 'iso_a5_148x210mm',    # A5 (148×210mm)
            'A6': 'iso_a6_105x148mm',    # A6 (105×148mm)

            # ISO 标准纸张 (B 系列)
            'B4': 'iso_b4_250x353mm',    # B4 (250×353mm)
            'B5': 'iso_b5_176x250mm',    # B5 (176×250mm)

            # 照片纸尺寸 (英寸)
            '3.5x5': 'na_index-3.5x5_3.5x5in',       # 3.5×5 英寸照片
            '4x6': 'na_index-4x6_4x6in',         # 4×6 英寸照片 (102×152mm)
            '5x7': 'na_5x7_5x7in',               # 5×7 英寸照片 (127×178mm)
            '8x10': 'na_8x10_8x10in',              # 8×10 英寸照片 (203×254mm)

        }
        # 获取纸张大小，如果不在映射表中则使用 A4 作为默认值
        cups_paper_size = paper_size_map.get(paper_size, 'iso_a4_210x297mm')
        cmd.extend(['-o', f'media={cups_paper_size}'])


        # 添加纸张材质设置
        # PWG 5101.1 标准介质类型映射（多品牌打印机兼容）
        paper_type_map = {
            # 标准类型 (PWG 5101.1)
            'plain': 'stationery',       # 普通纸
            'paper': 'stationery',       # 普通纸别名
            'normal': 'stationery',      # 普通纸别名
            'photo': 'photographic',     # 照片纸
            'glossy': 'photographic',    # 光面照片纸
            'matte': 'photographic',     # 哑光照片纸
            'envelope': 'envelope',      # 信封
            'transparency': 'transparency',  # 透明胶片
            'labels': 'labels',          # 标签纸
            'cardstock': 'cardstock',    # 卡片纸
            'auto': 'auto',              # 自动选择
        }
        cups_paper_type = paper_type_map.get(paper_type.lower(), 'stationery')
        cmd.extend(['-o', f'media-type={cups_paper_type}'])

        # 添加色彩设置
        if color_mode == 'mono':
            cmd.extend(['-o', 'print-color-mode=monochrome'])
        else:
            cmd.extend(['-o', 'print-color-mode=color'])
            
        # 添加双面打印设置
        if duplex == 'two-sided-long-edge':
            cmd.extend(['-o', 'sides=two-sided-long-edge'])
        elif duplex == 'two-sided-short-edge':
            cmd.extend(['-o', 'sides=two-sided-short-edge'])
        else:
            cmd.extend(['-o', 'sides=one-sided'])

        # 添加打印方向设置（纵向/横向）
        if orientation == 'landscape':
            cmd.extend(['-o', 'orientation-requested=4'])
        else:
            cmd.extend(['-o', 'orientation-requested=3'])

        # 页面范围已在文件处理时处理，不需要再传递给 CUPS
        # 注意：打印机可能不支持 page-ranges 属性，所以我们在文件层面处理
        
        # 添加打印缩放设置
        if print_scaling and print_scaling.strip():
            cmd.extend(['-o', f'print-scaling={print_scaling.strip()}'])

        # 添加镜像打印设置（水平翻转）
        if mirror:
            cmd.extend(['-o', 'mirror'])
        
        # 添加文件
        cmd.append(actual_print_file)
        
        # 记录打印命令（用于调试）
        logger.info(f"执行打印命令: {' '.join(cmd)}")
        logger.info(f"打印参数: color_mode={color_mode}, duplex={duplex}, orientation={orientation}, paper_size={paper_size}, paper_type={paper_type}, copies={copies}, page_range={page_range}")

        # 执行打印命令（强制 LC_ALL=C，确保 "request id is ..." 解析稳定，避免非英文 locale 下提取不到 CUPS 任务 ID）
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            env={**os.environ, 'LC_ALL': 'C'}
        )

        # 提取CUPS任务ID
        cups_job_id = None
        if result.returncode == 0:
            # 从输出中提取任务ID，格式通常是 "request id is PDF_Printer-1 (1 file(s))"
            output = result.stdout.strip()
            if 'request id' in output.lower():
                # 提取包含打印机名称和任务ID的部分
                words = output.split()
                for word in words:
                    # 查找格式为 "PrinterName-123" 的词
                    if '-' in word:
                        parts = word.split('-')
                        if len(parts) >= 2:
                            # 检查最后一部分是否为纯数字（任务ID）
                            potential_id = parts[-1]
                            if potential_id.isdigit():
                                cups_job_id = potential_id
                                break


        # 更新任务状态
        with print_jobs_lock:
            print_jobs[job_id] = {
                'id': job_id,
                'cups_job_id': cups_job_id,
                'filename': os.path.basename(filepath),
                'printer': printer_name,
                'color_mode': color_mode,
                'duplex': duplex,
                'orientation': orientation,
                'paper_size': paper_size,
                'paper_type': paper_type,
                'copies': copies,
                'page_range': page_range,
                'print_scaling': print_scaling,
                'mirror': mirror,
                'actual_print_file': actual_print_file,
                'status': 'submitted' if result.returncode == 0 else 'failed',
                'message': result.stdout if result.returncode == 0 else result.stderr,
                'timestamp': datetime.now().isoformat(),
                'progress': 0
            }

        if result.returncode == 0:
            # 启动后台线程监控进度
            logger.info(f"启动监控线程：job_id={job_id}, cups_job_id={cups_job_id}, printer_name={printer_name}")
            monitor_thread = threading.Thread(
                target=monitor_job_progress,
                args=(job_id, cups_job_id, printer_name)
            )
            monitor_thread.daemon = True
            monitor_thread.start()

        return job_id, result.returncode == 0

    except Exception as e:
        with print_jobs_lock:
            print_jobs[job_id] = {
                'id': job_id,
                'filename': os.path.basename(filepath),
                'printer': printer_name,
                'status': 'error',
                'message': str(e),
                'timestamp': datetime.now().isoformat(),
                'progress': 0
            }
        return job_id, False

def get_job_final_state(printer_name, cups_job_id):
    """
    查询 CUPS 历史队列确认任务的最终状态

    任务离开活动队列并不等于成功完成，可能是被取消或中止。
    通过 lpstat -l -W all 解析该任务的属性块，读取其中的 Alerts/Alert 字段判断：
    - 包含 cancel: 被取消（如 job-canceled-by-user）
    - 包含 abort: 被中止（如 job-aborted-by-system）
    - 其余（processing-to-stop-point / none / 空）: 正常完成
    - 两个队列都找不到该任务行: CUPS 重启或队列被清空，状态无法确认

    Returns:
        'completed' / 'cancelled' / 'failed' / 'unknown'
    """
    try:
        result = _lpstat(['-l', '-W', 'all', '-o', printer_name])
        if result.returncode != 0:
            return 'unknown'

        job_prefix = f"{printer_name}-{cups_job_id} "
        lines = result.stdout.split('\n')

        # 收集该任务属性块：从任务行开始，直到下一个非缩进行的下一任务/打印机即结束
        block = []
        found = False
        in_block = False
        for line in lines:
            s = line.strip()
            if s.startswith(job_prefix):
                found = True
                in_block = True
                block = [s]
                continue
            if in_block:
                if s == '' or (line and not line[0].isspace()):
                    break
                block.append(s)

        if not found:
            # 任务行都找不到（CUPS 重启 / 队列已清空），无法确认
            return 'unknown'

        # 在属性块中查找 Alerts/Alert 字段（兼容不同 CUPS 版本的大小写）
        alerts = ''
        for s in block:
            if s.startswith('Alerts:') or s.startswith('Alert:'):
                alerts = s.split(':', 1)[1].strip().lower()
                break

        if 'cancel' in alerts:
            return 'cancelled'
        if 'abort' in alerts:
            return 'failed'
        # 其余情况（processing-to-stop-point / none / 空）均视为正常完成
        return 'completed'
    except Exception as e:
        logger.error(f"查询任务最终状态失败：{e}")
        return 'unknown'


def monitor_job_progress(job_id, cups_job_id, printer_name):
    """
    监控打印任务进度（根据队列位置）

    每秒检查任务是否在 CUPS 队列中：
    - 不在队列：任务完成
    - 在队列中且为最小 job ID：处于队首，开始处理计时
    - 在队列中但不是最小 job ID：排队等待
    - 最大监控时间：10 分钟
    """
    start_time = time.time()
    processing_start_time = None
    max_monitor_time = 10 * 60

    while True:
        with print_jobs_lock:
            if job_id not in print_jobs:
                logger.debug(f"任务 {job_id} 不存在，停止监控")
                break
            job_status = print_jobs[job_id].get('status')

        if job_status == 'cancelled':
            logger.debug(f"任务 {job_id} 已取消，停止监控")
            break

        if cups_job_id is None:
            logger.warning(f"任务 {job_id} 缺少 CUPS job ID，无法跟踪进度，放弃监控")
            break

        elapsed_time = time.time() - start_time

        if elapsed_time >= max_monitor_time:
            with print_jobs_lock:
                if job_id in print_jobs:
                    print_jobs[job_id]['status'] = 'timeout'
                    print_jobs[job_id]['progress'] = 50
                    print_jobs[job_id]['message'] = f'监控超时（已运行{int(elapsed_time/60)}分钟）'
            logger.warning(f"任务 {job_id} 超时")
            break

        try:
            queue_result = _lpstat(['-o', printer_name])

            job_in_queue = (
                queue_result.returncode == 0 and
                any(line.strip().startswith(f"{printer_name}-{cups_job_id} ")
                    for line in queue_result.stdout.split('\n'))
            )

            if job_in_queue:
                # 解析队列中所有 CUPS job ID
                all_ids = []
                for line in queue_result.stdout.split('\n'):
                    m = re.match(rf'{re.escape(printer_name)}-(\d+)\s', line.strip())
                    if m:
                        all_ids.append(int(m.group(1)))

                current_cups_id = int(cups_job_id)
                is_front = all_ids and current_cups_id == min(all_ids)

                if is_front:
                    # 队首：开始或继续处理计时
                    if processing_start_time is None:
                        processing_start_time = time.time()
                    processing_elapsed = time.time() - processing_start_time
                    progress = min(90, int(processing_elapsed))
                    with print_jobs_lock:
                        if job_id in print_jobs:
                            print_jobs[job_id]['status'] = 'processing'
                            print_jobs[job_id]['progress'] = progress
                            print_jobs[job_id]['message'] = f'打印中... ({progress}%)'
                    logger.debug(f"任务 {job_id} 正在打印，进度：{progress}%")
                else:
                    # 排队中：重置处理计时，显示前方任务数
                    processing_start_time = None
                    sorted_ids = sorted(all_ids)
                    position = sorted_ids.index(current_cups_id)
                    with print_jobs_lock:
                        if job_id in print_jobs:
                            print_jobs[job_id]['status'] = 'queued'
                            print_jobs[job_id]['progress'] = 0
                            print_jobs[job_id]['message'] = f'排队中（前方 {position} 个任务）'
                    logger.debug(f"任务 {job_id} 排队中，前方 {position} 个任务")
            else:
                # 任务不在活动队列：确认最终状态（可能完成、被取消或被中止）
                final_state = get_job_final_state(printer_name, cups_job_id)
                if final_state == 'completed':
                    if processing_start_time is None:
                        processing_start_time = start_time
                    total_time = int(time.time() - processing_start_time)
                    # ★ 新增：打印任务"完成"后回查打印机真实状态
                    # 防止 cnijbe2 假成功、ipp 假成功等情况
                    error_info = _check_printer_after_job(printer_name)
                    # ★ 墨盒估算：记录打印页数供后续估算
                    try:
                        with print_jobs_lock:
                            job = print_jobs.get(job_id, {})
                            pages = int(job.get('copies', 1)) * 1  # 简化：copies 已经是份数 × pages；这里用 job.copies 直接
                        color_mode = (job.get('color_mode') or 'mono') if 'job' in dir() else 'mono'
                        # 用打印机的 URI 作 key
                        from ipp_client import record_print_pages
                        printer_uri = get_printer_uri(printer_name) or printer_name
                        # 估算页数（保守用 copies × 1，因为 PDF 的页数较难实时拿）
                        if error_info and not error_info.get('has_error'):
                            # 任务真正成功才记录（避免卡纸的任务污染估算）
                            record_print_pages(printer_uri, pages, color_mode)
                    except Exception as e:
                        logger.debug(f"记录墨盒估算页数失败: {e}")
                    with print_jobs_lock:
                        if job_id in print_jobs:
                            print_jobs[job_id]['status'] = 'completed'
                            print_jobs[job_id]['progress'] = 100
                            if error_info and error_info.get('has_error'):
                                # 打印虽然退出了 CUPS 队列，但打印机有错误
                                # 例如：cnijbe2 假成功、E04 卡纸但 cnijbe2 不知情等
                                print_jobs[job_id]['status'] = 'failed'
                                print_jobs[job_id]['progress'] = 0
                                print_jobs[job_id]['message'] = (
                                    f'打印完成但打印机报告错误：'
                                    f'{" / ".join(error_info["reasons_cn"])} '
                                    f'({", ".join(error_info["reasons"])})'
                                )
                                print_jobs[job_id]['printer_errors'] = error_info
                            else:
                                print_jobs[job_id]['message'] = f'打印完成 (耗时{total_time}秒)'
                    logger.info(f"任务 {job_id} 已完成，错误回查：{error_info}")
                    cleanup_temp_file(job_id)
                elif final_state == 'cancelled':
                    with print_jobs_lock:
                        if job_id in print_jobs:
                            print_jobs[job_id]['status'] = 'cancelled'
                            print_jobs[job_id]['progress'] = 0
                            print_jobs[job_id]['message'] = '打印任务已被取消，请检查打印机'
                    logger.info(f"任务 {job_id} 已被取消")
                elif final_state == 'failed':
                    with print_jobs_lock:
                        if job_id in print_jobs:
                            print_jobs[job_id]['status'] = 'failed'
                            print_jobs[job_id]['progress'] = 0
                            print_jobs[job_id]['message'] = '打印任务已中止，请检查打印机状态'
                    logger.warning(f"任务 {job_id} 已中止")
                else:
                    with print_jobs_lock:
                        if job_id in print_jobs:
                            print_jobs[job_id]['status'] = 'unknown'
                            print_jobs[job_id]['progress'] = 0
                            print_jobs[job_id]['message'] = '打印状态无法确认，请查看打印机输出'
                    logger.warning(f"任务 {job_id} 状态无法确认（可能 CUPS 重启或队列被清空）")
                break

        except Exception as e:
            logger.error(f"监控任务进度失败：{e}")
            time.sleep(10)
        else:
            time.sleep(1)


def cleanup_temp_file(job_id):
    """
    清理打印任务的临时文件

    只清理 /tmp 目录下的临时文件，previews 目录的文件保留
    """
    with print_jobs_lock:
        if job_id not in print_jobs:
            return
        actual_print_file = print_jobs[job_id].get('actual_print_file')
    
    if not actual_print_file:
        return

    # 只清理 /tmp 目录下的临时文件
    if actual_print_file.startswith('/tmp/'):
        try:
            if os.path.exists(actual_print_file):
                os.remove(actual_print_file)
                logger.info(f"已清理临时文件：{actual_print_file}")
        except Exception as e:
            logger.error(f"清理临时文件失败：{e}")


def get_printer_queue(printer_name):
    """获取特定打印机的队列信息"""
    queue = []
    status = "unknown"
    
    try:
        # 获取打印队列
        result = _lpstat(['-o'])
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split('\n')
            for line in lines:
                if line.strip():
                    # lpstat -o 输出格式: "printer-name-123    username    1024   filename"
                    parts = [part for part in line.split() if part]
                    if len(parts) >= 4:
                        # 第一部分是 printer-jobID
                        job_info = parts[0]
                        # 检查是否属于指定打印机
                        if job_info.startswith(f"{printer_name}-"):
                            queue.append({
                                'job_id': job_info.split('-')[-1],
                                'user': parts[1],
                                'size': parts[2],
                                'filename': ' '.join(parts[3:])
                            })
    except Exception as e:
        logger.error(f"获取打印队列失败: {e}")
    
    try:
        # 获取打印机状态
        printer_status = _lpstat(['-p', printer_name])
        if printer_status.returncode == 0:
            status_line = printer_status.stdout.strip()
            if "idle" in status_line.lower():
                status = "idle"
            elif "printing" in status_line.lower():
                status = "printing"
            elif "disabled" in status_line.lower():
                status = "disabled"
        else:
            logger.warning(f"获取打印机状态失败: {printer_status.stderr}")
    except Exception as e:
        logger.error(f"获取打印机状态失败: {e}")
    
    # 总是返回队列信息，即使状态获取失败
    return {
        'printer': printer_name,
        'status': status,
        'queue': queue,
        'queue_length': len(queue)
    }

@app.route('/')
@app.route('/zh')
def index():
    """中文主页（默认）"""
    trigger_update_check()
    return render_template('index.html',
        display_version=DISPLAY_VERSION,
        max_upload_size=app.config['MAX_CONTENT_LENGTH'],
        allowed_extensions=sorted(app.config['ALLOWED_EXTENSIONS']))

@app.route('/en')
def index_en():
    """English Home Page"""
    trigger_update_check()
    return render_template('index_en.html',
        display_version=DISPLAY_VERSION,
        max_upload_size=app.config['MAX_CONTENT_LENGTH'],
        allowed_extensions=sorted(app.config['ALLOWED_EXTENSIONS']))


@app.route('/api/update-check', methods=['GET'])
def api_update_check():
    """获取更新检查结果（供前端轮询）"""
    with update_check_lock:
        return jsonify({
            'status': update_check['status'],
            'local_version': APP_VERSION,
            'latest_version': update_check['latest'],
            'error': update_check['error'],
            'checked_at': update_check['checked_at'],
        })

@app.route('/api/printers', methods=['GET'])
def api_printers():
    """获取可用打印机列表（支持异步探测模式）"""
    # 始终使用快速模式（不进行在线探测），避免同步逐台探测阻塞请求
    printers = get_printers_fast()

    return jsonify({'printers': printers})


@app.route('/api/printer/<printer_name>/status', methods=['GET'])
def api_printer_status(printer_name):
    """获取单台打印机的在线状态（快速探测）"""
    try:
        # 获取单台打印机信息
        printer = get_single_printer_status(printer_name, timeout=5)
        if not printer:
            return jsonify({'error': '打印机不存在'}), 404
        
        return jsonify({
            'name': printer['name'],
            'status': printer['status'],
            'online_status': printer['online_status'],
            'uri': printer.get('uri')
        })
    except Exception as e:
        logger.error(f"获取打印机 {printer_name} 状态失败：{e}")
        return jsonify({
            'name': printer_name,
            'status': 'unknown',
            'online_status': 'unknown',
            'error': str(e)
        }), 500


@app.route('/api/printer/<printer_name>', methods=['GET'])
def api_printer_detail(printer_name):
    """获取单台打印机详细信息（墨盒、纸盒、队列信息）"""
    printer_data = {
        'name': printer_name,
        'status': 'unknown',
        'uri': None,
        'ink_cartridges': [],
        'trays': [],
        'queue': [],
        'ipp_status': None,
        'source': 'unknown'
    }

    # 获取打印机基本信息
    try:
        result = _lpstat(['-p', printer_name, '-v'])

        for line in result.stdout.split('\n'):
            match = re.search(r'device\s+for\s+' + re.escape(printer_name) + r':\s*(\S+)', line)
            if match:
                printer_uri = match.group(1).strip()
                printer_data['uri'] = printer_uri

                # 判断打印机类型
                # 支持的 IPP 端点格式：
                #   ipp:// / ipps:// / http:// / https://    直接 IPP
                #   dnssd://...                                由 ipp-usb/avahi 发布，需解析为实际 ipp:// 端点
                # 不支持：usb://、cnijbe2://、socket://、bjnp://（这些走私有协议，无法用 ipptool 查状态）
                ipp_target_uri = None
                if printer_uri.startswith(('ipp://', 'ipps://', 'http://', 'https://')):
                    ipp_target_uri = printer_uri
                elif printer_uri.startswith('dnssd://'):
                    # dnssd:// 服务名._ipp._tcp.local/?uuid=...
                    # ipp-usb/avahi 发布的队列走这个格式，需要解析成实际 ip 端口
                    resolved = _resolve_dnssd_uri(printer_uri)
                    if resolved:
                        ipp_target_uri = resolved
                        logger.debug(f"dnssd 解析：{printer_uri} -> {resolved}")

                if ipp_target_uri:
                    # IPP 打印机（直连或 dnssd），使用 ipptool 一次性获取所有信息
                    printer_data['status'] = 'idle'
                    if IPPTOOL_AVAILABLE:
                        try:
                            from ipp_client import get_all_printer_info_with_status
                            all_info = get_all_printer_info_with_status(ipp_target_uri)
                            printer_data['ink_cartridges'] = all_info.get('ink_cartridges', [])
                            printer_data['trays'] = all_info.get('trays', [])
                            printer_data['printer_info'] = all_info.get('printer_info', {})
                            printer_data['ipp_status'] = all_info.get('ipp_status', {})
                            if all_info.get('error'):
                                printer_data['source'] = f'ipptool 查询失败：{all_info.get("error")}'
                            else:
                                printer_data['source'] = f'ipptool: {ipp_target_uri}'
                        except Exception as e:
                            logger.error(f"通过 ipptool 获取打印机信息失败：{e}")
                            printer_data['source'] = 'ipptool（查询失败）'
                    else:
                        printer_data['source'] = 'ipptool 不可用'
                else:
                    # 私有协议（usb/cnijbe2/socket/bjnp）暂不通过 ipptool 查询
                    printer_data['status'] = 'idle'
                    printer_data['source'] = f'私有协议队列（{printer_uri.split("://")[0]}），不支持 IPP 状态查询'
                break
    except Exception as e:
        logger.error(f"获取打印机信息失败：{e}")
    
    # 获取打印队列
    queue_info = get_printer_queue(printer_name)
    printer_data['queue'] = queue_info.get('queue', [])
    if queue_info.get('status') != 'unknown':
        printer_data['status'] = queue_info.get('status')
    
    return jsonify(printer_data)

@app.route('/api/upload-cancel/<token>', methods=['DELETE'])
def api_upload_cancel(token):
    """取消上传，标记取消并清理已注册的文件（不 pop token，留给 api_upload 结束清理）"""
    with _upload_tokens_lock:
        entry = _upload_tokens.get(token)
        if not entry:
            return jsonify({'success': True})
        entry['cancelled'] = True
        files = list(entry['files'])
        entry['files'].clear()
    for path in files:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"已清理取消上传的残留文件：{path}")
        except Exception as e:
            logger.warning(f"清理残留文件失败：{path}, {e}")
    return jsonify({'success': True})


@app.route('/api/upload', methods=['POST'])
def api_upload():
    """上传文件"""
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400
    
    if file and allowed_file(file.filename):
        token = request.form.get('upload_token', '')
        if token:
            with _upload_tokens_lock:
                _upload_tokens[token] = {'cancelled': False, 'files': []}

        try:
            # 获取文件名和扩展名（使用自定义 safe_filename 保留中文等非ASCII字符）
            original_filename = file.filename
            filename = safe_filename(original_filename, app.config['ALLOWED_EXTENSIONS'])
            if not filename:
                return jsonify({'error': '不支持的文件类型'}), 400

            # 添加时间戳避免文件名冲突
            name, ext = os.path.splitext(filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{name}_{timestamp}{ext}"
            
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            if token and _register_upload_file(token, filepath):
                _cleanup_upload(token)
                return jsonify({'error': '上传已取消'}), 499
            
            # All file types: trigger conversion to PDF and generate preview images
            if is_document_file(filename) or ext.lower().endswith('.pdf') or is_image_file(filename):
                # Get base name for matching (remove original extension, keep timestamp)
                base_name = os.path.splitext(filename)[0]
                pdf_filename = f"{base_name}.pdf"

                conversion_warning = None

                # For Office documents, convert using LibreOffice
                if is_document_file(filename):
                    pdf_path = convert_to_pdf(filepath, app.config['PREVIEW_FOLDER'])
                    if not pdf_path:
                        logger.error(f"上传时文档转换失败：{filename} - LibreOffice 转换未生成 PDF 文件")
                        logger.error(f"原始文件路径：{filepath}")
                        logger.error(f"预期 PDF 路径：{os.path.join(app.config['PREVIEW_FOLDER'], pdf_filename)}")
                        conversion_warning = "文档转换失败，预览和打印将不可用，请检查 LibreOffice 是否安装或文档格式是否正确"
                    else:
                        if token and _register_upload_file(token, pdf_path):
                            _cleanup_upload(token)
                            return jsonify({'error': '上传已取消'}), 499
                        logger.info(f"文档转换成功，生成预览图片：{pdf_path}")
                        images = convert_pdf_to_images(pdf_path, app.config['PREVIEW_FOLDER'], pdf_filename=pdf_filename)
                        if token and images and _register_upload_files(token, images):
                            _cleanup_upload(token)
                            return jsonify({'error': '上传已取消'}), 499

                # For images: 不再转 PDF + 不再生成 PNG 缩略图。
                # 把原图复制到 previews/ 目录，前端用 <img> 直显（快、不依赖 PDF.js、不依赖 pdftoppm）。
                # 旧版：convert_image_to_pdf + convert_pdf_to_images；新版：原图直传。
                elif is_image_file(filename):
                    dest_filename = f"{base_name}{ext}"  # 保留原扩展名（png/jpg/...）
                    dest_path = os.path.join(app.config['PREVIEW_FOLDER'], dest_filename)
                    try:
                        shutil.copy2(filepath, dest_path)
                        # 注册为可预览源（前端 /api/preview/raw/<token> 会读它）
                        if token and _register_upload_file(token, dest_path):
                            _cleanup_upload(token)
                            return jsonify({'error': '上传已取消'}), 499
                        logger.info(f"图片原图复制成功（跳过 PDF 转换）：{dest_path}")
                        # pdf_filename 仍指向同名 .pdf 占位（旧前端流程兜底用，但不再实际写入）
                        pdf_filename = None
                        images = []
                    except Exception as copy_error:
                        logger.error(f"图片复制失败：{copy_error}")
                        conversion_warning = "图片复制失败，预览和打印将不可用"
                        images = []

                # For PDFs, copy to previews and generate images
                elif ext.lower().endswith('.pdf'):
                    pdf_path = os.path.join(app.config['PREVIEW_FOLDER'], pdf_filename)
                    if not os.path.exists(pdf_path):
                        try:
                            shutil.copy2(filepath, pdf_path)
                            if token and _register_upload_file(token, pdf_path):
                                _cleanup_upload(token)
                                return jsonify({'error': '上传已取消'}), 499
                            logger.info(f"PDF 复制成功：{pdf_path}")
                        except Exception as copy_error:
                            logger.error(f"PDF 复制失败：{copy_error}")
                            conversion_warning = "PDF 复制失败，预览和打印将不可用，请联系管理员"

                    if os.path.exists(pdf_path):
                        logger.info(f"PDF 文件，生成预览图片：{pdf_path}")
                        images = convert_pdf_to_images(pdf_path, app.config['PREVIEW_FOLDER'], pdf_filename=pdf_filename)
                        if token and images and _register_upload_files(token, images):
                            _cleanup_upload(token)
                            return jsonify({'error': '上传已取消'}), 499
                    else:
                        logger.warning(f"PDF 文件不存在：{pdf_filename}")
                        if not conversion_warning:
                            conversion_warning = "PDF 文件不存在，预览将不可用"

                # Get preview images (only if no conversion warning)
                preview_images = []
                if not conversion_warning:
                    # 图片类型：pdf_filename 为 None（图片不再转 PDF，原图直传），
                    # 跳过 PNG 缩略图查找。
                    if pdf_filename:
                        preview_images = get_preview_images(pdf_filename)
                        # 转换成功却未能生成任何预览图（如系统缺少 poppler-utils / pdftoppm），
                        # 给出明确警告，避免前端显示“上传成功”却看不到预览
                        if not preview_images:
                            conversion_warning = "预览图片生成失败，可能无法预览（请确认系统已安装 poppler-utils / pdftoppm）"
                    # 图片：preview_images 留空（前端会用 <img> + /api/preview/raw/<token> 加载）
            
            response_data = {
                'success': True,
                'filename': filename
            }
            if preview_images:
                response_data['preview_images'] = preview_images
                response_data['preview_images_count'] = len(preview_images)
            
            # Add warning if conversion failed
            if conversion_warning:
                response_data['warning'] = conversion_warning

            # 成功，清除 token
            if token:
                with _upload_tokens_lock:
                    _upload_tokens.pop(token, None)

            return jsonify(response_data)
        except Exception as e:
            if token:
                _cleanup_upload(token)
            return jsonify({'error': f'文件保存失败: {str(e)}'}), 500
    
    return jsonify({'error': '不支持的文件类型'}), 400


@app.route('/api/open-from-path', methods=['GET'])
def api_open_from_path():
    """从 fnOS 文件管理器"右键打开"接收文件路径。

    流程：
      1. 接收 ?path=/vol1/.../file.pdf 参数
      2. 安全检查（路径存在、可读、白名单前缀）
      3. 复制到 uploads/ 目录（带时间戳避免冲突）
      4. 走与 /api/upload 相同的处理（生成 token + 返回文件信息），前端自动进入预览界面
    """
    raw_path = request.args.get('path', '').strip()

    if not raw_path:
        return jsonify({'error': '缺少 path 参数'}), 400

    # 解码 URL 编码（fnOS 传入的 path 通常已 URL-encode）
    try:
        abs_path = urllib.parse.unquote(raw_path)
    except Exception:
        abs_path = raw_path

    # 安全检查 1：必须是 /vol1/ 开头（fnOS 用户共享目录）
    # 这是用户上传/共享文件的常见位置；fnOS 文件管理器传入的路径都应该在这里
    # 不允许访问 /etc、/var 等系统目录
    if not abs_path.startswith('/vol1/') and not abs_path.startswith('/vol2/') and not abs_path.startswith('/vol3/'):
        return jsonify({'error': f'安全限制：只允许访问 /vol1/ /vol2/ /vol3/ 共享目录'}), 403

    # 安全检查 2：规范化路径、禁止 .. 逃逸
    abs_path = os.path.abspath(abs_path)
    if '..' in abs_path.split(os.sep):
        return jsonify({'error': f'路径包含非法字符 ..'}), 403

    # 安全检查 3：文件必须存在且可读
    if not os.path.isfile(abs_path):
        return jsonify({'error': f'文件不存在或不是普通文件: {abs_path}'}), 404
    if not os.access(abs_path, os.R_OK):
        return jsonify({'error': f'文件不可读（权限不足）'}), 403

    # 安全检查 4：文件大小限制（与上传相同：100MB）
    file_size = os.path.getsize(abs_path)
    if file_size > app.config.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024):
        return jsonify({'error': f'文件过大（限制 100MB），当前 {file_size / 1024 / 1024:.1f}MB'}), 413

    # 安全检查 5：扩展名白名单
    ext = os.path.splitext(abs_path)[1].lstrip('.').lower()
    if not ext or ext not in app.config['ALLOWED_EXTENSIONS']:
        return jsonify({'error': f'不支持的文件格式 .{ext}'}), 415

    try:
        # 复制到 uploads/ 目录（带时间戳避免覆盖）
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_id = uuid.uuid4().hex[:8]
        # 保留原始文件名（但清理特殊字符）
        safe_base = os.path.basename(abs_path)
        # 移除不安全字符但保留中文
        safe_base = re.sub(r'[^\w.\-\u4e00-\u9fff]', '_', safe_base)
        new_filename = f"{timestamp}_{unique_id}_{safe_base}"
        dest_path = os.path.join(app.config['UPLOAD_FOLDER'], new_filename)

        shutil.copy2(abs_path, dest_path)

        # 生成上传 token
        upload_token = 'fp_' + datetime.now().strftime('%Y%m%d%H%M%S') + '_' + uuid.uuid4().hex[:8]
        _upload_tokens[upload_token] = {
            'filename': new_filename,
            'original_filename': os.path.basename(abs_path),
            'size': file_size,
            'total': file_size,
            'received': file_size,  # 已完整"上传"
            'start_time': time.time(),
            'finalize_event': threading.Event(),  # 立即就绪
        }
        _upload_tokens[upload_token]['finalize_event'].set()

        logger.info(f"从路径打开文件：{abs_path} -> {dest_path} ({file_size} bytes)")

        return jsonify({
            'success': True,
            'upload_token': upload_token,
            'filename': new_filename,
            'original_filename': os.path.basename(abs_path),
            'size': file_size,
            'from_path': True,
            'source_path': abs_path,
        })
    except Exception as e:
        logger.error(f"打开路径失败 {abs_path}: {e}")
        return jsonify({'error': f'打开文件失败: {str(e)}'}), 500


@app.route('/api/preview/<path:filename>', methods=['GET'])
def api_preview(filename):
    """获取文件预览"""
    try:
        # 安全检查：移除路径遍历字符
        filename = os.path.basename(filename)

        # 检查是否是预览图片（格式：{base_name}-{page}.png）
        # 预览图片保存在 previews/ 目录
        # 判断规则：文件名中包含 -数字.png 格式
        if re.search(r'-\d+\.png$', filename, re.IGNORECASE):
            # 预览图片在 previews/ 目录中
            image_path = get_safe_path(app.config['PREVIEW_FOLDER'], filename)
            if image_path and os.path.exists(image_path):
                ext = filename.rsplit('.', 1)[1].lower()
                mime_types = {
                    'jpg': 'image/jpeg',
                    'jpeg': 'image/jpeg',
                    'png': 'image/png',
                    'gif': 'image/gif',
                    'bmp': 'image/bmp',
                    'svg': 'image/svg+xml'
                }
                return send_from_directory(
                    app.config['PREVIEW_FOLDER'],
                    filename,
                    mimetype=mime_types.get(ext, 'application/octet-stream')
                )
            else:
                logger.warning(f"预览图片不存在：{filename}, 路径：{image_path}")
                return jsonify({'error': '预览图片不存在'}), 404
        
        # 对于 PDF 和文档文件，使用 get_preview_file 获取预览 PDF
        preview_file = get_preview_file(filename)
        
        if not preview_file:
            # 无法获取预览文件 - 可能是文件不存在或转换失败
            ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            convertible_extensions = ['doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'rtf', 'txt',
                                       'jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg']
            
            if ext in convertible_extensions:
                # 检查是否是文件不存在
                upload_path = get_safe_path(app.config['UPLOAD_FOLDER'], filename)
                if not upload_path or not os.path.exists(upload_path):
                    return jsonify({
                        'error': '文件不存在',
                        'message': f'文件 {filename} 不存在或已被删除'
                    }), 404
                else:
                    # 文件存在但预览不可用 - 预览文件不存在，需要重新上传
                    return jsonify({
                        'error': '预览文件不存在',
                        'message': f'文件 {filename} 的预览文件不存在，请重新上传该文件',
                        'solution': '请删除该文件后重新上传，系统将自动生成预览文件'
                    }), 500
            else:
                return jsonify({'error': '无法预览此文件'}), 404
        if os.path.exists(preview_file):
            # 获取文件所在的目录
            preview_dir = os.path.dirname(preview_file)
            preview_filename = os.path.basename(preview_file)
            
            # 根据文件类型设置Content-Type
            if preview_file.lower().endswith('.pdf'):
                # 手动创建响应，确保inline显示
                response = send_from_directory(
                    preview_dir, 
                    preview_filename, 
                    mimetype='application/pdf'
                )
                # 强制设置Content-Disposition为inline，移除filename参数
                response.headers['Content-Disposition'] = 'inline'
                return response
            else:
                # 图片文件
                ext = preview_file.rsplit('.', 1)[1].lower()
                mime_types = {
                    'jpg': 'image/jpeg',
                    'jpeg': 'image/jpeg',
                    'png': 'image/png',
                    'gif': 'image/gif',
                    'bmp': 'image/bmp',
                    'svg': 'image/svg+xml'
                }
                return send_from_directory(
                    preview_dir, 
                    preview_filename, 
                    mimetype=mime_types.get(ext, 'application/octet-stream')
                )
        else:
            return jsonify({'error': '预览文件不存在'}), 404
    except Exception as e:
        logger.error(f"获取预览失败: {e}")
        return jsonify({'error': f'预览失败: {str(e)}'}), 500

@app.route('/api/files', methods=['GET'])
def api_list_files():
    """获取已上传的文件列表"""
    try:
        files = []
        upload_folder = app.config['UPLOAD_FOLDER']

        if os.path.exists(upload_folder):
            for filename in os.listdir(upload_folder):
                filepath = os.path.join(upload_folder, filename)
                if os.path.isfile(filepath):
                    # 获取文件信息
                    stat = os.stat(filepath)
                    # 内联文件类型判断
                    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
                    if ext in ['pdf']:
                        file_type = 'pdf'  # lowercase for frontend comparison
                    elif ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg']:
                        file_type = 'image'  # lowercase for frontend comparison
                    elif ext in ['txt', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'rtf']:
                        file_type = 'document'  # lowercase for frontend comparison
                    else:
                        file_type = 'other'  # lowercase for frontend comparison
                    # Get preview images for document, image and PDF files
                    preview_images = []
                    if file_type == 'document' or file_type == 'image' or file_type == 'pdf':
                        # Use full filename with timestamp
                        pdf_filename = os.path.splitext(filename)[0] + '.pdf'
                        preview_images = get_preview_images(pdf_filename)
                    
                    file_info = {
                        'filename': filename,
                        'size': stat.st_size,
                        'mtime': stat.st_mtime,
                        'type': file_type
                    }
                    if preview_images:
                        file_info['preview_images'] = preview_images
                        file_info['preview_images_count'] = len(preview_images)
                    files.append(file_info)

        # 按修改时间倒序排列（最新的在前）
        files.sort(key=lambda x: x['mtime'], reverse=True)

        return jsonify({'success': True, 'files': files})
    except Exception as e:
        logger.error(f"获取文件列表失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/<path:filename>', methods=['DELETE'])
def api_delete_file(filename):
    """删除上传的文件"""
    # 获取安全的文件路径
    filepath = get_safe_path(app.config['UPLOAD_FOLDER'], filename)
    
    if not filepath:
        logger.warning(f"非法文件路径访问尝试: {filename}")
        return jsonify({'error': '非法文件路径'}), 403
    
    # 检查是否有正在进行的打印任务
    with print_jobs_lock:
        for job_id, job in print_jobs.items():
            if job['filename'] == filename:
                if job['status'] in ['submitted', 'processing', 'queued']:
                    logger.warning(f"文件 {filename} 正在打印中，无法删除 (任务状态：{job['status']})")
                    return jsonify({
                        'error': f'文件正在打印中，无法删除 (任务状态：{job["status"]})'
                    }), 400
    
    if os.path.exists(filepath):
        try:
            # 删除原始文件
            os.remove(filepath)
            
            # 同时删除对应的预览文件（如果存在）
            # 对于文档文件和 PDF 文件，预览文件会保存在 PREVIEW_FOLDER
            name, ext = os.path.splitext(filename)
            # 检查是否需要删除预览文件（文档、图片或 PDF）
            is_document = is_document_file(filename)
            is_image = is_image_file(filename)
            is_pdf = ext.lower().endswith('.pdf')
            if is_document or is_image or is_pdf:
                # 预览文件是转换后的 PDF
                # Use consistent naming with api_print()
                base_name = os.path.splitext(filename)[0]
                preview_pdf_filename = f"{base_name}.pdf"
                preview_pdf = get_safe_path(app.config['PREVIEW_FOLDER'], preview_pdf_filename)
                if preview_pdf and os.path.exists(preview_pdf):
                    try:
                        os.remove(preview_pdf)
                        logger.info(f"已删除预览文件：{preview_pdf}")
                    except Exception as e:
                        logger.warning(f"删除预览文件失败：{e}")

            # Delete preview images (generated by pdftoppm)
            # Images are named: {filename}-1.png, {filename}-2.png, ...
            image_base_name = os.path.splitext(filename)[0]  # Keep timestamp
            # Use glob.escape to handle special characters in filename
            image_pattern = os.path.join(app.config['PREVIEW_FOLDER'], f"{glob.escape(image_base_name)}-*.png")
            image_files = glob.glob(image_pattern)
            for img_file in image_files:
                try:
                    os.remove(img_file)
                    logger.info(f"已删除预览图片：{img_file}")
                except Exception as e:
                    logger.warning(f"删除预览图片失败：{e}")

            # 清理打印时可能产生的 /tmp 抽取页 PDF（extract_pdf_pages_to_tmp 生成，
            # 文件名形如 print_{base}_{uuid}_pages_{range}.pdf），避免残留
            tmp_pattern = f"/tmp/print_{glob.escape(image_base_name)}_*"
            for tmp_file in glob.glob(tmp_pattern):
                try:
                    os.remove(tmp_file)
                    logger.info(f"已清理临时抽取文件：{tmp_file}")
                except Exception as e:
                    logger.warning(f"清理临时抽取文件失败：{tmp_file}, {e}")

            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"删除文件失败: {e}")
            return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': '文件不存在'}), 404

def validate_page_range(page_range):
    """验证页面范围格式"""
    # 允许的格式：1, 1-5, 1-5 8, 1-5 8 10-12（空格分隔，不支持逗号）
    # pdftk 语法要求：空格分隔多个页面范围，如 "1-5 8 10-12"
    if ',' in page_range:
        return False
    pattern = r'^(\d+(-\d+)?)(\s+\d+(-\d+)?)*$'
    return bool(re.match(pattern, page_range))

@app.route('/api/print', methods=['POST'])
def api_print():
    """提交打印任务"""
    logger.info("=" * 80)
    logger.info("接收到打印请求")

    # 记录原始数据
    logger.info(f"原始数据: {request.data}")

    try:
        data = request.json
        logger.info(f"解析后的JSON: {json.dumps(data, indent=2, ensure_ascii=False)}")
    except Exception as e:
        logger.error(f"JSON解析失败: {e}")
        return jsonify({'error': '请求数据格式错误'}), 400

    filename = data.get('filename', data.get('filepath'))
    if filename:
        filename = os.path.basename(filename)
    filepath = get_safe_path(app.config['UPLOAD_FOLDER'], filename) if filename else None
    if not filepath:
        logger.warning(f"非法文件路径访问尝试: {filename}")
        return jsonify({'error': '非法文件路径'}), 403

    printer_name = data.get('printer')
    color_mode = data.get('color_mode', 'mono')  # 默认改为mono（黑白）
    duplex = data.get('duplex', 'one-sided')
    orientation = data.get('orientation', 'portrait')
    paper_size = data.get('paper_size', 'A4')
    paper_type = data.get('paper_type', 'plain')
    copies = data.get('copies', 1)  # Default to int, not string
    page_range = data.get('page_range', None)
    
    # 空字符串视为 None（无页面范围限制）
    if page_range is not None and not page_range.strip():
        page_range = None
    
    mirror = data.get('mirror', False)
    print_scaling = data.get('print_scaling', 'fit')

    # 记录解析后的参数
    logger.info(f"解析后的参数:")
    logger.info(f"  filename: {filename}")
    logger.info(f"  filepath: {filepath}")
    logger.info(f"  printer_name: {printer_name}")
    logger.info(f"  color_mode: {color_mode}")
    logger.info(f"  duplex: {duplex}")
    logger.info(f"  orientation: {orientation}")
    logger.info(f"  paper_size: {paper_size}")
    logger.info(f"  paper_type: {paper_type}")
    logger.info(f"  copies: {copies}")
    logger.info(f"  page_range: {page_range}")
    logger.info(f"  mirror: {mirror}")
    logger.info(f"  print_scaling: {print_scaling}")

    # 基本参数验证
    if not filepath or not printer_name:
        return jsonify({'error': '缺少必要参数'}), 400

    # 防御性校验打印机名格式（即使后续 CUPS 列表校验被跳过，也拒绝畸形名，避免异常值传入 lp）
    if not re.match(r'^[A-Za-z0-9_.\-]+$', printer_name or ''):
        logger.warning(f"打印机名包含非法字符：{printer_name}")
        return jsonify({'error': '打印机名称包含非法字符'}), 400

    # 校验打印机必须存在于 CUPS 打印机列表（防止提交到不存在的队列/注入）
    try:
        available_printers = {p['name'] for p in get_printers_fast()}
        if available_printers and printer_name not in available_printers:
            logger.warning(f"打印请求使用了不存在的打印机：{printer_name}")
            return jsonify({'error': f'打印机不存在：{printer_name}'}), 400
    except Exception as e:
        # 获取打印机列表失败时放行（fail-open），避免偶发子进程错误影响正常打印
        logger.warning(f"校验打印机列表失败，跳过校验：{e}")

    if not os.path.exists(filepath):
        return jsonify({'error': '文件不存在'}), 404
    
    # 注意：预览文件检查在 get_printable_file() 中进行，避免重复检查
    # 验证纸张大小
    valid_paper_sizes = ['A4', 'A3', 'A2', 'A1', 'A5', 'A6', 'B4', 'B5', '3.5x5', '4x6', '5x7', '8x10']
    if paper_size not in valid_paper_sizes:
        return jsonify({'error': f'无效的纸张大小，支持的格式: {", ".join(valid_paper_sizes)}'}), 400

    # 验证纸张材质
    valid_paper_types = ['plain', 'photo', 'glossy', 'matte', 'envelope', 'cardstock', 'labels', 'auto']
    if paper_type not in valid_paper_types:
        return jsonify({'error': f'无效的纸张材质，支持的类型: {", ".join(valid_paper_types)}'}), 400

    # 验证打印份数
    try:
        copies = int(copies)
        if copies < 1 or copies > 99:
            return jsonify({'error': '打印份数必须在1-99之间'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': '打印份数必须是数字'}), 400

    # 验证色彩模式
    if color_mode not in ['color', 'mono']:
        return jsonify({'error': '色彩模式无效，必须是 color 或 mono'}), 400

    # 验证双面设置
    if duplex not in ['one-sided', 'two-sided-long-edge', 'two-sided-short-edge']:
        return jsonify({'error': '双面设置无效'}), 400

    # 验证打印方向
    if orientation not in ['portrait', 'landscape']:
        return jsonify({'error': '打印方向无效，必须是 portrait 或 landscape'}), 400

    # 验证打印缩放设置
    valid_print_scalings = ['none', 'fill', 'fit', 'auto-fit', 'auto']
    if print_scaling not in valid_print_scalings:
        return jsonify({'error': f'无效的打印缩放，支持的格式：{", ".join(valid_print_scalings)}'}), 400

    # 验证页面范围格式（可选）
    if page_range and page_range.strip():
        if not validate_page_range(page_range.strip()):
            return jsonify({'error': '页面范围格式无效，请使用空格分隔（如：1-5 8 10-12）'}), 400
        page_range = page_range.strip()

    # 推荐 IPP 队列提示（不强制切换，仅提示）
    recommendation = _recommend_ipp_queue(printer_name)

    job_id, success = submit_print_job(filepath, printer_name, color_mode, duplex, orientation, paper_size, paper_type, copies, page_range, mirror, print_scaling)

    if success:
        _sweep_terminal_jobs()   # 提交即清理：print_jobs 只在打印时增长，此处足以维持上限
        with print_jobs_lock:
            job_data = _sanitize_job(print_jobs[job_id])
        resp = {
            'success': True,
            'job_id': job_id,
            'job': job_data
        }
        if recommendation:
            resp['recommendation'] = recommendation
        return jsonify(resp)
    else:
        with print_jobs_lock:
            error_message = print_jobs[job_id]['message']
        return jsonify({
            'success': False,
            'error': error_message
        }), 500

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def api_cancel_job(job_id):
    """取消打印任务或删除已完成/失败的任务"""
    # 检查任务是否存在（加锁读取）
    with print_jobs_lock:
        if job_id not in print_jobs:
            return jsonify({'error': '任务不存在'}), 404
        job = print_jobs[job_id].copy()

    # 检查任务状态
    if job['status'] in ['completed', 'failed', 'cancelled']:
        # 已完成、失败或已取消的任务，先清理临时文件，再删除
        cleanup_temp_file(job_id)
        with print_jobs_lock:
            if job_id in print_jobs:
                del print_jobs[job_id]
                logger.info(f"任务 {job_id} 已删除（状态：{job['status']}）")
        return jsonify({
            'success': True,
            'message': '任务记录已删除'
        })

    # 检查任务是否已经结束（错误状态）
    if job['status'] == 'error':
        return jsonify({'error': f'任务处于错误状态，无法取消'}), 400

    # 保存当前进度
    current_progress = job.get('progress', 0)

    def remove_job_after_delay(job_id, delay_seconds=5):
        """延时删除任务，先清理临时文件"""
        time.sleep(delay_seconds)
        # 删除前先清理临时文件
        cleanup_temp_file(job_id)
        with print_jobs_lock:
            if job_id in print_jobs:
                del print_jobs[job_id]
                logger.info(f"任务 {job_id} 已从列表中删除")

    # 尝试取消 CUPS 任务
    if job.get('cups_job_id'):
        try:
            result = subprocess.run(
                ['cancel', job['cups_job_id']],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, 'LC_ALL': 'C'}
            )

            if result.returncode == 0:
                with print_jobs_lock:
                    if job_id in print_jobs:
                        print_jobs[job_id]['status'] = 'cancelled'
                        print_jobs[job_id]['progress'] = current_progress
                        print_jobs[job_id]['message'] = f'任务已取消 (用户手动取消)'
                # 清理临时文件
                cleanup_temp_file(job_id)
                # 启动后台线程，5 秒后删除任务
                remove_thread = threading.Thread(target=remove_job_after_delay, args=(job_id, 5))
                remove_thread.daemon = True
                remove_thread.start()
                return jsonify({
                    'success': True,
                    'message': '打印任务已取消，5 秒后从列表删除'
                })
            else:
                # CUPS 取消失败，但本地标记为已取消
                logger.warning(f"CUPS 取消任务失败：{result.stderr}")
                with print_jobs_lock:
                    if job_id in print_jobs:
                        print_jobs[job_id]['status'] = 'cancelled'
                        print_jobs[job_id]['progress'] = current_progress
                        print_jobs[job_id]['message'] = f'任务已取消 (CUPS 取消失败，但本地已标记为取消)'
                # 清理临时文件
                cleanup_temp_file(job_id)
                # 启动后台线程，5 秒后删除任务
                remove_thread = threading.Thread(target=remove_job_after_delay, args=(job_id, 5))
                remove_thread.daemon = True
                remove_thread.start()
                return jsonify({
                    'success': True,
                    'message': '任务已标记为取消（CUPS 可能已完成），5 秒后从列表删除'
                })
        except Exception as e:
            logger.error(f"取消任务异常：{e}")
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    else:
        # 如果没有 cups_job_id，直接标记为取消
        with print_jobs_lock:
            if job_id in print_jobs:
                print_jobs[job_id]['status'] = 'cancelled'
                print_jobs[job_id]['progress'] = current_progress
                print_jobs[job_id]['message'] = f'任务已取消 (用户手动取消)'
        # 启动后台线程，5 秒后删除任务
        remove_thread = threading.Thread(target=remove_job_after_delay, args=(job_id, 5))
        remove_thread.daemon = True
        remove_thread.start()
        return jsonify({
            'success': True,
            'message': '任务已标记为取消，5 秒后从列表删除'
        })

@app.route('/api/jobs', methods=['GET'])
def api_all_jobs():
    """获取所有任务"""
    with print_jobs_lock:
        jobs = [_sanitize_job(j) for j in print_jobs.values()]
    return jsonify({'jobs': jobs})

@app.route('/api/printer-queue/<printer_name>', methods=['GET'])
def api_printer_queue(printer_name):
    """获取特定打印机的队列信息"""
    queue_info = get_printer_queue(printer_name)
    return jsonify(queue_info)


@app.route('/api/printer/<printer_name>/ink/reset', methods=['POST'])
def api_ink_reset(printer_name):
    """
    重置墨盒估算基准
    当用户更换新墨盒时，前端调这个 API 告诉后端"假设 100%"，后续按打印页数估算
    请求体: {"level": 100, "markers": ["Color", "Black"]}  # markers 可选，默认所有
    """
    try:
        data = request.json or {}
        level = int(data.get('level', 100))
        markers = data.get('markers')  # None 表示所有
        from ipp_client import _update_ink_baseline
        # get_printer_uri 是 app.py 本地的，不是 ipp_client 的
        uri = get_printer_uri(printer_name) or printer_name
        # 拉一次当前墨盒列表（不知道 marker_names 就拿不到）
        if not markers:
            from ipp_client import get_all_printer_info_with_status
            info = get_all_printer_info_with_status(uri)
            markers = [c['name'] for c in info.get('ink_cartridges', [])]
        if not markers:
            return jsonify({'error': '未找到墨盒列表'}), 400
        for m in markers:
            _update_ink_baseline(uri, m, level)
        return jsonify({'success': True, 'updated': markers, 'baseline': level})
    except Exception as e:
        logger.error(f"重置墨盒估算失败: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("Web 打印服务启动中...")
    print("=" * 60)
    print(f"服务地址：http://localhost:5000")
    print(f"上传目录：{os.path.abspath(app.config['UPLOAD_FOLDER'])}")
    print("=" * 60)

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
