#!/usr/bin/env python3
"""
通过IPP协议获取打印机墨盒和纸盒信息
使用 ipptool 命令行工具提取信息
"""

import subprocess
import logging
import re
import tempfile
import os

logger = logging.getLogger(__name__)

# 检查 ipptool 是否可用
def check_ipptool_available():
    """检查 ipptool 命令是否可用"""
    try:
        result = subprocess.run(
            ['which', 'ipptool'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False

IPPTOOL_AVAILABLE = check_ipptool_available()

# ============================================================================
# 优化的属性列表 - 只获取必要的数据（方案 B）
# ============================================================================

# 墨盒相关属性
NEEDED_MARKER_ATTRIBUTES = [
    'marker-names',
    'marker-colors',
    'marker-types',
    'marker-levels',
    'marker-high-levels',
    'marker-low-levels'
]

# 纸盒相关属性
NEEDED_TRAY_ATTRIBUTES = [
    'printer-input-tray',
    'media-ready'
]

# 打印机基本信息属性
NEEDED_INFO_ATTRIBUTES = [
    'printer-info',
    'printer-up-time',
    'printer-firmware-version',
    'printer-make-and-model',
    'printer-state'
]

# 打印机状态属性
NEEDED_STATUS_ATTRIBUTES = [
    'printer-state',           # 与 NEEDED_INFO_ATTRIBUTES 重复，会自动去重
    'printer-state-reasons',
    'printer-alert',
    'printer-alert-description',
    'printer-state-message'
]

# 墨盒估算状态（用于 IPP 返回 -2 时仍能给出有参考价值的余量）
# key: (printer_uuid, marker_name) → {"baseline_level": int, "pages_printed_after": int, "color_mode": str, "last_update": iso}
# Canon TS3380 等机型在用兼容墨盒时 IPP 总返回 -2，需靠"打印页数×消耗系数"推算
import threading
import json
import os

# 状态持久化文件（放在 previews 目录：容器挂载 host 可写，且不在 uploads 文件列表 API 中）
_INK_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'previews', '.ink_estimate_state.json')

_ink_estimate_state = {}
_ink_estimate_lock = threading.Lock()


def _load_ink_state():
    """启动时从 JSON 文件加载估算状态（跨容器重启保持）"""
    try:
        if os.path.exists(_INK_STATE_FILE):
            with open(_INK_STATE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            _ink_estimate_state.clear()
            for k, v in raw.items():
                # key 序列化为 "printer|marker"
                if '|' in k:
                    uri, name = k.split('|', 1)
                    _ink_estimate_state[(uri, name)] = v
    except Exception as e:
        logger.debug(f"加载墨盒估算状态失败：{e}")


def _save_ink_state():
    """将估算状态写回 JSON 文件"""
    try:
        dirpath = os.path.dirname(_INK_STATE_FILE)
        os.makedirs(dirpath, exist_ok=True)
        with _ink_estimate_lock:
            payload = {f"{k[0]}|{k[1]}": v for k, v in _ink_estimate_state.items()}
        with open(_INK_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.debug(f"保存墨盒估算状态失败：{e}")


_load_ink_state()

# 墨盒每张 A4 消耗估算（基于典型 5% 覆盖率）
# 实际部署可根据打印机型号调整；这里给一个保守估计
INK_CONSUMPTION_PER_PAGE = {
    'mono': 0.4,       # 黑白：A4 文本约 0.4% / 页
    'color': 1.5,      # 彩色：彩色 A4 约 1.5% / 页（影响 4 个墨盒各一部分）
    'photo': 4.0,      # 照片纸：约 4% / 页
}

# 合并所有需要的属性（自动去重）
ALL_NEEDED_ATTRIBUTES = list(set(
    NEEDED_MARKER_ATTRIBUTES + 
    NEEDED_TRAY_ATTRIBUTES + 
    NEEDED_INFO_ATTRIBUTES + 
    NEEDED_STATUS_ATTRIBUTES
))

# 生成 requested-attributes 字符串
ALL_NEEDED_ATTRIBUTES_STR = ','.join(ALL_NEEDED_ATTRIBUTES)


def get_all_printer_info_with_status(printer_url):
    """
    一次性获取打印机所有信息（墨盒、纸盒、基本信息、状态属性）
    使用自定义测试文件，只获取必要的 17 个属性，减少响应数据量
    
    Args:
        printer_url: 打印机 URL，如 "ipp://192.168.1.100:631/ipp/print"
        
    Returns:
        字典包含所有信息：
        {
            'ink_cartridges': [...],      # 墨盒信息
            'trays': [...],               # 纸盒信息
            'printer_info': {...},        # 打印机基本信息
            'ipp_status': {...},          # IPP 状态属性
            'raw_output': '...',          # 原始输出（用于调试）
            'error': None                 # 错误信息（如果有）
        }
    """
    if not IPPTOOL_AVAILABLE:
        logger.warning("ipptool 不可用，无法获取打印机信息")
        return {
            'ink_cartridges': [],
            'trays': [],
            'printer_info': {},
            'ipp_status': None,
            'raw_output': '',
            'error': 'ipptool not available'
        }
    
    try:
        # 生成自定义测试文件（只请求必要的 17 个属性）
        test_content = f"""{{
    NAME "Get-All-Printer-Info"
    OPERATION Get-Printer-Attributes
    GROUP operation
    ATTR charset attributes-charset utf-8
    ATTR language attributes-natural-language en
    ATTR uri printer-uri {printer_url}
    ATTR keyword requested-attributes {ALL_NEEDED_ATTRIBUTES_STR}
}}
"""
        # 写入临时文件
        fd, test_file = tempfile.mkstemp(suffix='.test')
        try:
            os.write(fd, test_content.encode('utf-8'))
            os.close(fd)
            
            # 执行 ipptool
            cmd = ['ipptool', '-tv', printer_url, test_file]
            logger.debug(f"执行命令：{' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,  # 15 秒超时
                env={**os.environ, 'LC_ALL': 'C'}  # 强制英文输出，避免 locale 影响解析
            )
            
            if result.returncode != 0:
                logger.error(f"ipptool 执行失败：{result.stderr}")
                return {
                    'ink_cartridges': [],
                    'trays': [],
                    'printer_info': {},
                    'ipp_status': None,
                    'raw_output': '',
                    'error': result.stderr.strip()
                }
            
            output = result.stdout
            logger.debug(f"ipptool 输出长度：{len(output)} 字节")
            
            # 解析各类信息（独立 try-except，部分失败不影响其他）
            try:
                ink_cartridges = _parse_ink_cartridges(output, printer_uri=printer_url)
            except Exception as e:
                logger.warning(f"解析墨盒信息失败：{e}")
                ink_cartridges = []
            
            try:
                trays = _parse_trays(output)
            except Exception as e:
                logger.warning(f"解析纸盒信息失败：{e}")
                trays = []
            
            try:
                printer_info = _parse_printer_info(output)
            except Exception as e:
                logger.warning(f"解析打印机基本信息失败：{e}")
                printer_info = {}
            
            try:
                ipp_status = _parse_printer_status(output)
            except Exception as e:
                logger.warning(f"解析 IPP 状态失败：{e}")
                ipp_status = None
            
            return {
                'ink_cartridges': ink_cartridges,
                'trays': trays,
                'printer_info': printer_info,
                'ipp_status': ipp_status,
                'raw_output': output,
                'error': None
            }
            
        finally:
            # 清理临时文件
            try:
                os.unlink(test_file)
            except OSError:
                pass
                
    except subprocess.TimeoutExpired:
        logger.error("获取打印机信息超时")
        return {
            'ink_cartridges': [],
            'trays': [],
            'printer_info': {},
            'ipp_status': None,
            'raw_output': '',
            'error': 'timeout'
        }
    except Exception as e:
        logger.error(f"获取打印机信息失败：{e}")
        return {
            'ink_cartridges': [],
            'trays': [],
            'printer_info': {},
            'ipp_status': None,
            'raw_output': '',
            'error': str(e)
        }


def _parse_ink_cartridges(output, printer_uri=None):
    """
    从 ipptool 输出中解析墨盒信息
    遵循 IPP RFC 3805 标准处理 marker-levels 特殊值
    当 IPP 返回 -2（余量不可读）但有 cartridge 装着时，尝试基于"上次已知余量 + 打印页数"估算

    Args:
        output: ipptool 输出文本
        printer_uri: 打印机 URI（用于估算的 state key）

    Returns:
        墨盒信息列表
    """
    marker_names = _parse_ipp_attribute(output, 'marker-names')
    marker_colors = _parse_ipp_attribute(output, 'marker-colors')
    marker_types = _parse_ipp_attribute(output, 'marker-types')
    marker_levels_raw = _parse_ipp_attribute(output, 'marker-levels')

    # 将 marker-levels 转换为整数（IPP 返回的是整数值，但 ipptool 输出为字符串）
    marker_levels = []
    for v in marker_levels_raw:
        try:
            marker_levels.append(int(v))
        except (ValueError, TypeError):
            marker_levels.append(-1)

    # 判断是否有合法的墨盒名称（避免打印机报告"无墨盒"时的误报）
    # 如果 marker-types 全是 'unknown' 或名称是占位符，认为是"未装"
    valid_types = [t for t in marker_types if t and t != 'unknown']
    has_real_cartridge = bool(marker_names) and bool(valid_types)

    # ★ Canon PG-545/CL-546 风格：把"三色一体"墨盒（marker-colors 是 "#00CFFF#F200FF#FFDA00"）
    #   拆成 cyan/magenta/yellow 三个虚拟墨盒显示，更直观
    ink_cartridges = []
    for i in range(len(marker_names)):
        name = marker_names[i] if i < len(marker_names) else f'墨盒 {i+1}'
        color = marker_colors[i] if i < len(marker_colors) else 'unknown'
        level_type = marker_types[i] if i < len(marker_types) else 'unknown'

        # 获取原始级别值
        raw_level = marker_levels[i] if i < len(marker_levels) else -1

        # 三色一体墨盒（颜色字段含 3 个 #，如 Canon CL-546 "#00CFFF#F200FF#FFDA00"）
        is_tri_color = color.startswith('#') and color.count('#') == 3
        sub_names = ['cyan', 'magenta', 'yellow']
        sub_color_hex = ['#06b6d4', '#ec4899', '#eab308']

        # 内部函数：根据单个墨盒名解析状态（考虑三色墨盒每个子色独立查询）
        def _resolve_level_state(resolve_name):
            if not has_real_cartridge:
                return ('not_installed', None)
            if 0 <= raw_level <= 100:
                # 真实读数：更新该名称基准（同时用于三色时的子名）
                _update_ink_baseline(printer_uri, resolve_name, raw_level)
                return ('normal', raw_level)
            # -1 / -2 / -3：估算路径
            estimated = _estimate_ink_level(printer_uri, resolve_name)
            if estimated is not None:
                return ('estimated', estimated)
            return ('unknown', None)

        if is_tri_color:
            # 三色墨盒：拆成 C/M/Y 三个子墨盒
            for j, (sub_name, sub_hex) in enumerate(zip(sub_names, sub_color_hex)):
                child_key = f'{name}-{sub_name}'
                l_status, l_value = _resolve_level_state(child_key)
                # 若子名无基准，尝试用父名兜底（兼容旧状态）
                if l_status == 'unknown':
                    _l_status2, _l_value2 = _resolve_level_state(name)
                    if _l_status2 != 'unknown':
                        l_status, l_value = _l_status2, _l_value2
                ink_cartridges.append({
                    'name': child_key,
                    'display_name': sub_name[0].upper(),  # 只显示首字母: C/M/Y
                    'full_display_name': sub_name.upper(),  # 完整名: CYAN...
                    'color': sub_hex,
                    'type': level_type,
                    'level': l_value,
                    'level_status': l_status,
                    'raw_level': raw_level,
                    'has_cartridge': has_real_cartridge,
                    'group': name,  # 标记属于哪个物理墨盒
                })
        else:
            # 黑色或其他单色墨盒
            l_status, l_value = _resolve_level_state(name)
            # 单色墨盒也尽量用首字母：黑色用打印惯例 K
            is_black = (str(color).lower() in ('#000000', 'black', '#000')) or ('black' in str(name).lower())
            dname = 'K' if is_black else str(name)[:1].upper()
            ink_cartridges.append({
                'name': name,
                'display_name': dname,
                'full_display_name': name if not is_black else 'Black',
                'color': color,
                'type': level_type,
                'level': l_value,
                'level_status': l_status,
                'raw_level': raw_level,
                'has_cartridge': has_real_cartridge,
            })

    logger.debug(f"提取到 {len(ink_cartridges)} 个墨盒信息（已拆分三色一体）")
    return ink_cartridges


def _ink_state_key(printer_uri, marker_name):
    """生成墨盒估算状态 key"""
    return (printer_uri or 'default', marker_name or 'unknown')


def _update_ink_baseline(printer_uri, marker_name, level):
    """
    当 IPP 返回真实余量时，更新"基准"
    后续若 IPP 报 -2，从基准扣减打印页数
    """
    if level is None or level < 0:
        return
    key = _ink_state_key(printer_uri, marker_name)
    with _ink_estimate_lock:
        existing = _ink_estimate_state.get(key, {})
        # 如果新值 < 已存值，才覆盖（说明墨盒正在消耗）；避免新值>旧值时误判（可能是加墨或重置）
        if level <= existing.get('baseline_level', 100):
            _ink_estimate_state[key] = {
                'baseline_level': level,
                'pages_printed_after': 0,  # 重置打印页数计数
                'last_update': __import__('datetime').datetime.now().isoformat(),
            }
    # 持久化（在锁外保存避免长时间持锁）
    _save_ink_state()


def _estimate_ink_level(printer_uri, marker_name):
    """
    估算墨盒余量
    算法：基于上次真实读数，扣减此后累计的打印页数 × 消耗系数

    Returns:
        int 0-100 估算余量，如果无法估算（无基准）返回 None
    """
    key = _ink_state_key(printer_uri, marker_name)
    with _ink_estimate_lock:
        state = _ink_estimate_state.get(key)
    if not state:
        return None  # 没有基准，无法估算

    baseline = state.get('baseline_level', 0)
    pages = state.get('pages_printed_after', 0)
    color_mode = state.get('color_mode', 'mono')

    # 单次消耗（取最大可能的值，给保守估计）
    # 因为无法精确知道是 mono/color/photo 模式，用各模式的平均值
    # 实际部署可让用户选择打印模式来精确估算
    consumption_per_page = (
        INK_CONSUMPTION_PER_PAGE.get(color_mode, 0.4)
        if color_mode != 'mixed'
        else (INK_CONSUMPTION_PER_PAGE['mono'] + INK_CONSUMPTION_PER_PAGE['color']) / 2
    )

    estimated = max(0, baseline - pages * consumption_per_page)
    return int(round(estimated))


def record_print_pages(printer_uri, pages, color_mode='mono'):
    """
    记录一次打印的页数（供墨盒估算使用）
    当打印任务成功完成时由外部调用

    Args:
        printer_uri: 打印机 URI
        pages: 实际打印页数
        color_mode: 'mono' / 'color' / 'photo'
    """
    if pages <= 0:
        return

    # 从 printer_uri 解析 printer_id（用 hostname:port 作为 key）
    with _ink_estimate_lock:
        # 找出这台打印机所有墨盒 key，更新页数
        prefix = printer_uri or 'default'
        for key in list(_ink_estimate_state.keys()):
            if key[0] == prefix:
                state = _ink_estimate_state[key]
                state['pages_printed_after'] = state.get('pages_printed_after', 0) + pages
                state['color_mode'] = color_mode
                state['last_update'] = __import__('datetime').datetime.now().isoformat()
    # 持久化
    _save_ink_state()


def _parse_trays(output):
    """
    从 ipptool 输出中解析纸盒信息
    
    Args:
        output: ipptool 输出文本
        
    Returns:
        纸盒信息列表
    """
    printer_tray_info = _parse_printer_input_tray(output)
    media_ready_list = _parse_ipp_attribute(output, 'media-ready')
    media_ready = ', '.join(media_ready_list) if media_ready_list else None
    
    # 根据状态码映射中文/英文状态
    status_cn_map = {
        '3': '空',
        '4': '已装载',
        '5': '可用',
        '6': '移除'
    }
    status_en_map = {
        '3': 'Empty',
        '4': 'Loaded',
        '5': 'Available',
        '6': 'Removed'
    }
    
    trays = []
    for i, tray_info in enumerate(printer_tray_info):
        # 兼容内联格式（name/type/status）与 collection 格式（tray-name/tray-type/tray-status）
        name = tray_info.get('name') or tray_info.get('tray-name') or f'纸盒 {i+1}'
        tray_type = tray_info.get('type') or tray_info.get('tray-type') or 'unknown'
        status = tray_info.get('status') or tray_info.get('tray-status') or 'unknown'
        status_cn = status_cn_map.get(status, '未知')
        status_en = status_en_map.get(status, status)
        tray_media = tray_info.get('media') or tray_info.get('media-ready') or media_ready
        
        trays.append({
            'name': name,
            'type': tray_type,
            'status': status,
            'status_cn': status_cn,
            'status_en': status_en,
            'media_ready': tray_media
        })
    
    # IPP Everywhere / 现代驱动常不暴露 printer-input-tray，导致纸盒列表为空。
    # 此时用 media-ready 合成纸盒条目（每个就绪介质视为一个可用纸盒）。
    if not trays and media_ready_list:
        for media in media_ready_list:
            trays.append({
                'name': media,
                'type': 'auto',
                'status': '5',
                'status_cn': '可用',
                'status_en': 'Available',
                'media_ready': media
            })
    
    logger.debug(f"提取到 {len(trays)} 个纸盒信息")
    return trays


def _parse_printer_info(output):
    """
    从 ipptool 输出中解析打印机基本信息
    
    Args:
        output: ipptool 输出文本
        
    Returns:
        字典包含打印机基本信息
    """
    printer_info = {}
    
    # printer-info
    printer_info_match = re.search(r'printer-info\s*\([^)]+\)\s*=\s*([^\n]+)', output)
    if printer_info_match:
        printer_info['printer_info'] = printer_info_match.group(1).strip()
    
    # printer-make-and-model
    make_model_match = re.search(r'printer-make-and-model\s*\([^)]+\)\s*=\s*([^\n]+)', output)
    if make_model_match:
        printer_info['printer_make_and_model'] = make_model_match.group(1).strip()
    
    # printer-up-time (秒转小时)
    uptime_match = re.search(r'printer-up-time\s*\([^)]+\)\s*=\s*(\d+)', output)
    if uptime_match:
        uptime_seconds = int(uptime_match.group(1))
        uptime_hours = round(uptime_seconds / 3600, 2)
        printer_info['printer_up_time_hours'] = uptime_hours
        printer_info['printer_up_time_seconds'] = uptime_seconds
    
    # printer-firmware-version
    firmware_match = re.search(r'printer-firmware-version\s*\([^)]+\)\s*=\s*([^\n]+)', output)
    if firmware_match:
        printer_info['printer_firmware_version'] = firmware_match.group(1).strip()
    
    logger.debug(f"打印机信息：{printer_info}")
    return printer_info


def _parse_ipp_attribute(output, attribute_name):
    """
    从 ipptool 输出中解析 IPP 属性

    Args:
        output: ipptool 输出文本
        attribute_name: 属性名称

    Returns:
        属性值列表
    """
    pattern = rf'^{re.escape(attribute_name)}\s*\([^)]+\)\s*=\s*(.*)$'
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith(attribute_name + ' '):
            match = re.match(pattern, line)
            if match:
                values_str = match.group(1)
                # 分割值（按逗号），过滤空值
                values = [v.strip() for v in values_str.split(',') if v.strip()]
                return values

    logger.debug(f"未找到属性: {attribute_name}")
    return []

def _parse_printer_input_tray(output):
    """
    从 ipptool 输出中解析 printer-input-tray

    支持两种输出格式：
    - octetString 内联格式（单行，多个纸盒用 ;, 分隔）：
        printer-input-tray (1setOf octetString) = type=other;...;name=auto;,type=...
    - collection 多行格式（每个纸盒一个 { ... } 块）：
        printer-input-tray (1setOf collection) =
            {
                tray-name (nameWithoutLanguage) = "Tray 1"
                tray-type (type2Keyword) = "stationery"
                tray-status (type2Enum) = 4
            }

    Args:
        output: ipptool 输出文本

    Returns:
        纸盒信息列表（键值对字典）
    """
    lines = output.split('\n')
    start_line = None
    for i, line in enumerate(lines):
        if re.match(r'printer-input-tray\s*\(', line.strip()):
            start_line = i
            break

    if start_line is None:
        logger.debug("未找到 printer-input-tray 属性")
        return []

    first_stripped = lines[start_line].strip()
    _, _, first_value = first_stripped.partition('= ')
    is_collection = not first_value.strip()

    if is_collection:
        # collection 格式：解析连续的 { ... } 块
        blocks = []
        current_block = []
        in_block = False
        for line in lines[start_line + 1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == '{':
                in_block = True
                current_block = []
                continue
            if stripped == '}':
                if in_block:
                    blocks.append(current_block)
                in_block = False
                continue
            if not in_block:
                # 遇到下一个属性，结束解析
                if re.match(r'[\w][\w-]*\s*\(', stripped):
                    break
                continue
            current_block.append(stripped)

        trays = []
        for block in blocks:
            tray_info = {}
            for line in block:
                match = re.match(r'([\w-]+)\s*\([^)]+\)\s*=\s*(.*)$', line)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip().strip('"')
                    tray_info[key] = value
            if tray_info:
                trays.append(tray_info)
        return trays

    # 内联格式：收集值行（值可能跨多行）
    value_parts = []
    in_value = False
    for line in lines[start_line:]:
        stripped = line.strip()
        if not in_value:
            if re.match(r'printer-input-tray\s*\(', stripped):
                _, _, rest = stripped.partition('= ')
                if rest:
                    value_parts.append(rest)
                in_value = True
        else:
            if not stripped or re.match(r'[\w][\w-]*\s*\(', stripped):
                break
            value_parts.append(stripped)

    values_str = ' '.join(value_parts)

    # 分割多个纸盒（按 ;, 分隔）
    trays = []
    for tray_str in values_str.split(';,'):
        # 解析键值对
        tray_info = {}
        for kv in tray_str.split(';'):
            if '=' in kv:
                key, value = kv.split('=', 1)
                tray_info[key.strip()] = value.strip()
        trays.append(tray_info)

    return trays


def _parse_printer_status(output):
    """
    从 ipptool 输出中解析打印机 IPP 状态属性
    
    Args:
        output: ipptool 输出文本
        
    Returns:
        字典包含状态信息：
        {
            'printer_state': 'idle',
            'printer_state_reasons': ['none'],
            'printer_alert': None,
            'printer_alert_description': None,
            'printer_state_message': None
        }
    """
    def extract_attr_value(attr_name, output_text):
        """提取属性值，逐行匹配"""
        for line in output_text.split('\n'):
            line = line.strip()
            if line.startswith(attr_name + ' '):
                match = re.search(rf'^{re.escape(attr_name)}\s*\([^)]+\)\s*=\s*(.*)$', line)
                if match:
                    return match.group(1).strip()
        return None
    
    # 提取各个属性
    printer_state_raw = extract_attr_value('printer-state', output)
    printer_state_reasons_raw = extract_attr_value('printer-state-reasons', output)
    printer_alert_raw = extract_attr_value('printer-alert', output)
    printer_alert_description_raw = extract_attr_value('printer-alert-description', output)
    printer_state_message_raw = extract_attr_value('printer-state-message', output)
    
    # 解析 printer-state
    printer_state = printer_state_raw
    
    # 解析 printer-state-reasons（转换为列表）
    printer_state_reasons = []
    if printer_state_reasons_raw:
        printer_state_reasons = [r.strip() for r in printer_state_reasons_raw.split(',')]
    
    # 解析 printer-alert
    printer_alert = printer_alert_raw
    
    # 解析 printer-alert-description
    printer_alert_description = printer_alert_description_raw
    
    # 解析 printer-state-message
    printer_state_message = printer_state_message_raw
    
    return {
        'printer_state': printer_state,
        'printer_state_reasons': printer_state_reasons,
        'printer_alert': printer_alert,
        'printer_alert_description': printer_alert_description,
        'printer_state_message': printer_state_message
    }


def get_printer_errors(printer_uri, timeout=5):
    """
    快速取打印机错误状态（用于打印后回查）
    只请求 4 个关键属性，超时 5 秒，避免阻塞主流程

    Args:
        printer_uri: ipp://host:port/ipp/print 或 http(s)://...
        timeout: 超时（秒）

    Returns:
        dict: {
            'available': bool,             # 是否能成功连上打印机
            'printer_state': str|None,     # idle/processing/stopped
            'printer_state_reasons': list, # ['media-jam'] 等
            'printer_alert_description': str|None,
            'error': str|None,             # 网络/超时等错误
        }
    """
    if not IPPTOOL_AVAILABLE:
        return {
            'available': False,
            'printer_state': None,
            'printer_state_reasons': [],
            'printer_alert_description': None,
            'error': 'ipptool 不可用',
        }

    try:
        test_content = """{
    NAME "Get-Errors-Quickly"
    OPERATION Get-Printer-Attributes
    GROUP operation
    ATTR charset attributes-charset utf-8
    ATTR language attributes-natural-language en
    ATTR uri printer-uri """ + printer_uri + """
    ATTR keyword requested-attributes printer-state,printer-state-reasons,printer-alert,printer-alert-description
}
"""
        fd, test_file = tempfile.mkstemp(suffix='.test')
        try:
            os.write(fd, test_content.encode('utf-8'))
            os.close(fd)

            # 用 -tv (verbose) 才能拿到属性值；-t 只输出 PASS/FAIL
            # 但 -tv 输出大，加 -T 限制超时
            result = subprocess.run(
                ['ipptool', '-tv', '-T', str(timeout), printer_uri, test_file],
                capture_output=True, text=True, timeout=timeout + 2,
                env={**os.environ, 'LC_ALL': 'C'}
            )

            if result.returncode != 0:
                # 解析 stdout 里是否有 PASS（说明连接成功但返回码非 0，比如某些属性缺失）
                if '[PASS]' in result.stdout:
                    pass  # 继续走解析
                else:
                    return {
                        'available': False,
                        'printer_state': None,
                        'printer_state_reasons': [],
                        'printer_alert_description': None,
                        'error': result.stderr.strip() or 'ipptool 调用失败',
                    }

            # 复用现有的解析器（_parse_printer_status）
            output = result.stdout
            status = _parse_printer_status(output)

            return {
                'available': True,
                'printer_state': status.get('printer_state'),
                'printer_state_reasons': status.get('printer_state_reasons', []) or [],
                'printer_alert_description': status.get('printer_alert_description'),
                'error': None,
            }
        finally:
            try:
                os.unlink(test_file)
            except OSError:
                pass

    except subprocess.TimeoutExpired:
        return {
            'available': False,
            'printer_state': None,
            'printer_state_reasons': [],
            'printer_alert_description': None,
            'error': 'timeout',
        }
    except Exception as e:
        return {
            'available': False,
            'printer_state': None,
            'printer_state_reasons': [],
            'printer_alert_description': None,
            'error': str(e),
        }


# ============================================================================
# IPP 错误码 → 用户操作指引 映射
# ============================================================================
# 主流厂商（Canon/HP/Epson/Brother）的"机器侧错误码"和 IPP 标准
# printer-state-reasons 的对应关系不总是 1:1。
# 当 IPP 报某个 reason 时，下面的字典给出用户在打印机面板/机器上可能看到的
# 错误码 + 该如何处理。

PRINTER_ERROR_GUIDE = {
    # ===== 缺纸 =====
    'media-empty':           {'code': 'E02/E03', 'cn': '纸张用完或未正确放入',          'action': '请放入 A4 纸（普通纸）到后部纸盘，确认纸张导轨已卡紧。'},
    'media-empty-error':     {'code': 'E02/E03', 'cn': '纸张用完或未正确放入',          'action': '请放入 A4 纸到后部纸盘。如果纸已放入，请取出后重新对齐放入。'},
    'media-needed':          {'code': 'E02',     'cn': '需要装入指定纸张',              'action': '请装入与打印设置匹配的纸张（参考屏幕上的纸张尺寸提示）。'},
    'media-needed-error':    {'code': 'E02',     'cn': '装入的纸张尺寸不匹配',          'action': '请装入与打印设置一致的纸张，或修改打印设置。'},

    # ===== 卡纸 =====
    'media-jam':             {'code': 'E04',     'cn': '卡纸',                          'action': '请打开打印机前盖，慢慢拉出卡住的纸张（顺着出纸方向），关闭前盖后重试。'},
    'media-jam-error':       {'code': 'E04',     'cn': '卡纸',                          'action': '请检查后部纸盘和出纸口，移除所有卡纸和碎纸。'},
    'media-jam-needed':      {'code': 'E04',     'cn': '卡纸',                          'action': '请清除卡纸，并装入纸张后重试。'},

    # ===== 墨水/墨盒 =====
    'marker-supply-empty':           {'code': 'E05/E07', 'cn': '墨盒已空',                'action': '请更换对应颜色的墨盒。打开前盖，等待墨盒支架停止后，按下旧墨盒取出并装入新墨盒。'},
    'marker-supply-empty-error':     {'code': 'E05/E07', 'cn': '墨盒已空',                'action': '请更换对应颜色的墨盒。'},
    'marker-supply-low':             {'code': '警告',     'cn': '墨水余量低',              'action': '建议提前准备备用墨盒，提示出现时不影响继续打印。'},
    'marker-supply-low-warning':     {'code': '警告',     'cn': '墨水余量低',              'action': '建议提前准备备用墨盒。'},
    'marker-waste-full':             {'code': 'E08',     'cn': '废墨收集器已满',          'action': '请联系 Canon 售后或经销商处理（废墨收集器用户无法自行更换）。'},
    'marker-failure':                {'code': 'E07',     'cn': '墨盒未正确安装或损坏',    'action': '请取出墨盒重新安装；如果仍报错，请更换新墨盒。'},

    # ===== 盖子/门 =====
    'cover-open':            {'code': 'E14',     'cn': '前盖未关闭',                    'action': '请关闭打印机前盖（听到咔哒声表示已合上）。'},
    'cover-open-error':      {'code': 'E14',     'cn': '前盖未关闭',                    'action': '请关闭打印机前盖。'},
    'door-open':             {'code': 'E14',     'cn': '机门打开',                      'action': '请关闭所有机门。'},
    'door-open-error':       {'code': 'E14',     'cn': '机门打开',                      'action': '请关闭所有机门。'},

    # ===== 纸盘/出纸 =====
    'input-tray-missing':    {'code': 'E15',     'cn': '纸盘缺失',                      'action': '请检查后部纸盘是否正确安装。'},
    'input-tray-empty':      {'code': 'E02',     'cn': '指定纸盘为空',                  'action': '请在指定的纸盘装入纸张。'},
    'output-area-full':      {'code': 'E13',     'cn': '出纸区纸张已满',                'action': '请取出出纸口的打印纸张，避免堵纸。'},
    'output-area-almost-full': {'code': '警告',   'cn': '出纸区快满',                    'action': '建议及时取走出纸口的纸张。'},
    'output-tray-missing':   {'code': 'E15',     'cn': '出纸盘缺失',                    'action': '请检查出纸盘是否正确安装。'},

    # ===== 打印头/对齐 =====
    'cleaner-time-out':      {'code': 'E16',     'cn': '打印头清洁超时',                'action': '请执行打印头清洁（打印机维护菜单）。如果仍报错，请联系售后。'},

    # ===== 通信 =====
    'connecting-to-device':  {'code': '通讯中',   'cn': '正在连接设备',                  'action': '请稍候，或检查 USB/Wi-Fi 连接。'},
    'timeout':               {'code': '通讯超时', 'cn': '通讯超时',                      'action': '请检查打印机与 NAS 的连接，重启打印机。'},
    'shutdown':              {'code': '关闭中',   'cn': '打印机正在关闭',                'action': '请重新开启打印机电源。'},

    # ===== 状态 =====
    'paused':                {'code': '已暂停',   'cn': '打印机已暂停',                  'action': '请在打印机控制面板恢复打印。'},
    'none':                  {'code': '',         'cn': '正常',                          'action': ''},
}


def get_error_guide(reason):
    """
    根据 IPP printer-state-reasons 获取用户操作指引

    Args:
        reason: 标准 IPP 错误关键词，如 'media-jam'

    Returns:
        dict: {'code': 'E04', 'cn': '卡纸', 'action': '...'}
        未知 reason 时返回 None
    """
    return PRINTER_ERROR_GUIDE.get(reason)


def get_error_guides(reasons):
    """
    批量获取错误指引，自动去重 'none'

    Args:
        reasons: list of IPP reason keywords

    Returns:
        list of {'code', 'cn', 'action', 'reason'}
    """
    guides = []
    for r in reasons or []:
        if r == 'none':
            continue
        g = PRINTER_ERROR_GUIDE.get(r)
        if g:
            guides.append({'reason': r, **g})
        else:
            # 未知错误，透传原始 reason
            guides.append({'reason': r, 'code': '?', 'cn': r, 'action': '请查看打印机屏幕或重启打印机。'})
    return guides