"""
打印预览相关路由 (Ported from cups-web, hanxi/cups-web)
========================================================

功能：
- /api/convert            将文档/图片/文本转成 PDF（流式回传，供前端 PDF.js 预览）
- /api/preview/file       临时缓存上传文件，返回 token，供前端访问
- /api/preview/pdf/<tok>  通过 token 取回 PDF（流式 application/pdf）
- /api/preview/cleanup    主动清理临时缓存

设计要点：
- 完全复用现有 upload/preview 目录与转换函数（convert_to_pdf / convert_image_to_pdf）
- 不破坏现有 /api/preview/<filename> 接口（向后兼容）
- token-UUID 临时缓存，TTL = 30 分钟，自动过期清理
- 大文件（>50MB）拒绝转换以避免 LibreOffice OOM
"""
import os
import io
import uuid
import time
import threading
import logging
from collections import OrderedDict
from flask import Blueprint, request, jsonify, send_file, Response

logger = logging.getLogger(__name__)

preview_bp = Blueprint('preview', __name__)

# ─── 临时文件缓存 ──────────────────────────────────────────────────────────────
# token -> {path, expires_at, mimetype, original_name}
_PREVIEW_CACHE = OrderedDict()
_PREVIEW_LOCK = threading.Lock()

# TTL：30 分钟自动过期（秒）
PREVIEW_TTL = 30 * 60
# 缓存最大数量（防止磁盘被撑爆）
PREVIEW_CACHE_MAX = 64
# 单文件大小上限（复用 app.config['MAX_CONTENT_LENGTH']，但此处给一个硬上限）
MAX_CONVERT_BYTES = 100 * 1024 * 1024  # 100MB


def _gc_cache():
    """清理过期条目；超过 PREVIEW_CACHE_MAX 时按 LRU 淘汰"""
    now = time.time()
    with _PREVIEW_LOCK:
        # 1) 过期淘汰
        expired = [k for k, v in _PREVIEW_CACHE.items() if v['expires_at'] < now]
        for k in expired:
            entry = _PREVIEW_CACHE.pop(k, None)
            if entry:
                _safe_remove(entry['path'])
        # 2) 数量上限淘汰
        while len(_PREVIEW_CACHE) > PREVIEW_CACHE_MAX:
            k, entry = _PREVIEW_CACHE.popitem(last=False)  # FIFO
            _safe_remove(entry['path'])


def _safe_remove(path):
    """安全删除文件，路径必须在 uploads 或 previews 目录下"""
    try:
        if not path:
            return
        real = os.path.realpath(path)
        # 仅允许删除 uvicorn 信任目录下的文件
        for base in ('uploads', 'previews', '/tmp'):
            base_real = os.path.realpath(os.path.join(os.path.dirname(__file__), base)) \
                if base != '/tmp' else '/tmp'
            if real.startswith(base_real + os.sep):
                if os.path.exists(real):
                    os.remove(real)
                return
        logger.warning(f"拒绝删除非信任路径：{real}")
    except Exception as e:
        logger.warning(f"删除临时文件失败：{e}")


def _store_temp_file(content_bytes, original_name):
    """把字节内容存到 previews 临时目录，返回 token"""
    _gc_cache()
    safe_name = os.path.basename(original_name or 'preview')
    # 文件命名：<token>__<safe_name>
    token = uuid.uuid4().hex
    preview_dir = os.path.join(os.path.dirname(__file__), 'previews')
    os.makedirs(preview_dir, exist_ok=True)
    path = os.path.join(preview_dir, f"_tmp_{token}__{safe_name}")
    with open(path, 'wb') as f:
        f.write(content_bytes)

    entry = {
        'path': path,
        'expires_at': time.time() + PREVIEW_TTL,
        'original_name': safe_name,
    }
    with _PREVIEW_LOCK:
        _PREVIEW_CACHE[token] = entry
    return token, path


# ─── 路由 ─────────────────────────────────────────────────────────────────────

@preview_bp.route('/api/preview/file', methods=['POST'])
def api_preview_file():
    """
    接收文件（multipart 'file' 字段），存入临时缓存，返回 token。

    响应：
        { token, expires_in, size, name }
    """
    try:
        if 'file' not in request.files:
            return jsonify({'error': '缺少 file 字段'}), 400
        f = request.files['file']
        if not f.filename:
            return jsonify({'error': '文件名为空'}), 400

        # 读取字节（限制大小）
        content = f.read()
        if len(content) > MAX_CONVERT_BYTES:
            return jsonify({'error': f'文件过大（>{MAX_CONVERT_BYTES//1024//1024}MB），无法预览'}), 413

        token, path = _store_temp_file(content, f.filename)
        logger.info(f"预览临时文件已缓存：token={token[:8]}…, size={len(content)}, name={f.filename}")
        return jsonify({
            'token': token,
            'expires_in': PREVIEW_TTL,
            'size': len(content),
            'name': f.filename,
        })
    except Exception as e:
        logger.error(f"预览文件缓存失败：{e}")
        return jsonify({'error': f'预览文件缓存失败：{e}'}), 500


@preview_bp.route('/api/preview/file', methods=['DELETE'])
def api_preview_file_delete():
    """通过 token 删除缓存。接受 query string、JSON body 或 form 字段"""
    token = request.args.get('token')
    if not token and request.is_json:
        try:
            token = (request.json or {}).get('token')
        except Exception:
            token = None
    if not token:
        token = request.form.get('token')
    if not token:
        return jsonify({'error': '缺少 token'}), 400
    with _PREVIEW_LOCK:
        entry = _PREVIEW_CACHE.pop(token, None)
        if entry:
            _safe_remove(entry['path'])
            return jsonify({'ok': True})
        return jsonify({'error': 'token 不存在或已过期'}), 404


@preview_bp.route('/api/preview/by-name', methods=['GET'])
def api_preview_by_name():
    """
    根据已上传文件名直接拿预览 token（供前端 selectFile 走预览路径）

    GET /api/preview/by-name?filename=xxx

    Returns:
        { token, name, size } 或 404

    路径策略：
      1. 图片（jpg/png/gif/bmp/svg）：直接返回原图 token（前端走 /api/preview/raw/<token>）
         不要找同名 .pdf，否则前端拿到 .pdf 后用 PDF.js 渲染图片 PDF 会失败
      2. PDF：直接返回 PDF token
      3. Office 文档：优先复用 previews/ 里已转换的 {base}.pdf；找不到则用 uploads 里的原文件
         （前端 handleFileByName 会去跑 convert_to_pdf）
    """
    from flask import request
    filename = request.args.get('filename', '').strip()
    if not filename:
        return jsonify({'error': 'filename 必填'}), 400

    # 安全检查：禁止路径遍历
    if '/' in filename or '\\' in filename or filename.startswith('.'):
        return jsonify({'error': '非法文件名'}), 400

    script_dir = os.path.dirname(__file__)
    previews_dir = os.path.join(script_dir, 'previews')
    uploads_dir = os.path.join(script_dir, 'uploads')

    lower = filename.lower()

    # ── 图片：直接返回原图 token（前端走 /api/preview/raw/<token>） ────────────
    if _is_image_name(filename):
        # 优先 previews/ 里的同名原图（v3.0.8 起图片上传时直接复制到这里）
        img_in_previews = os.path.join(previews_dir, filename)
        if os.path.exists(img_in_previews) and os.path.getsize(img_in_previews) > 0:
            chosen_path = img_in_previews
            source = 'previews-image'
        else:
            # 回退到 uploads 目录
            chosen_path = os.path.join(uploads_dir, filename)
            if not os.path.exists(chosen_path):
                return jsonify({'error': f'文件不存在：{filename}'}), 404
            source = 'uploads'
        with open(chosen_path, 'rb') as f:
            content = f.read()
        token, _ = _store_temp_file(content, filename)
        return jsonify({'token': token, 'name': filename, 'size': len(content),
                        'source': source})

    # ── PDF：直接返回 PDF token ─────────────────────────────────────────────
    if lower.endswith('.pdf'):
        # 优先 previews，再 uploads
        candidates = [
            (os.path.join(previews_dir, filename), 'previews-cached'),
            (os.path.join(uploads_dir, filename), 'uploads'),
        ]
        for path, source in candidates:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, 'rb') as f:
                    content = f.read()
                token, _ = _store_temp_file(content, filename)
                return jsonify({'token': token, 'name': filename, 'size': len(content),
                                'source': source})
        return jsonify({'error': f'文件不存在：{filename}'}), 404

    # ── Office 文档：优先复用 previews/ 里已转换的 PDF，找不到回退 uploads ────
    pdf_name = os.path.splitext(filename)[0] + '.pdf'
    pdf_path = os.path.join(previews_dir, pdf_name)
    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
        with open(pdf_path, 'rb') as f:
            content = f.read()
        token, _ = _store_temp_file(content, pdf_name)
        return jsonify({'token': token, 'name': pdf_name, 'size': len(content),
                        'source': 'previews-cached'})

    # uploads 里找原文件
    src_path = os.path.join(uploads_dir, filename)
    if not os.path.exists(src_path):
        return jsonify({'error': f'文件不存在：{filename}'}), 404

    with open(src_path, 'rb') as f:
        content = f.read()
    token, _ = _store_temp_file(content, filename)
    return jsonify({'token': token, 'name': filename, 'size': len(content),
                    'source': 'uploads'})


@preview_bp.route('/api/preview/pdf/<token>', methods=['GET'])
def api_preview_pdf(token):
    """
    根据 token 读取临时文件：
    - 已是 PDF：原样流式回传 application/pdf
    - 图片：调用 convert_image_to_pdf 转 PDF 后流式回传
    - Office/文本：调用 convert_to_pdf (LibreOffice) 转 PDF 后流式回传

    响应：
        application/pdf 流
    """
    with _PREVIEW_LOCK:
        entry = _PREVIEW_CACHE.get(token)
    if not entry:
        return jsonify({'error': 'token 不存在或已过期'}), 404

    src_path = entry['path']
    src_name = entry['original_name']

    if not os.path.exists(src_path):
        return jsonify({'error': '文件已被清理'}), 410

    # 已是 PDF：直接流
    if src_name.lower().endswith('.pdf'):
        return send_file(
            src_path,
            mimetype='application/pdf',
            as_attachment=False,
            conditional=True,  # 支持 Range（PDF.js 可能用到）
        )

    # 图片：快速路径——让前端去 /api/preview/raw/<token> 直接拿原图，
    # 避免每次预览都跑 img2pdf + pdftoppm 这条慢路径。
    # 如果前端忽略此 415 错误（老代码路径），就回退到旧的 PDF 转换流程。
    if _is_image_name(src_name):
        accept = (request.headers.get('Accept') or '').lower()
        # 浏览器 <img> 默认 Accept 含 image/*；PDF.js 取 PDF 则是 application/pdf
        wants_pdf = 'application/pdf' in accept and 'image/*' not in accept
        if not wants_pdf:
            return jsonify({
                'error': '图片请使用 /api/preview/raw/<token>',
                'raw_url': f'/api/preview/raw/{token}'
            }), 415

        # 老路径：转 PDF 流式回传（保留兼容）
        try:
            from app import convert_image_to_pdf  # 延迟导入避免循环
        except ImportError:
            convert_image_to_pdf = None
        if convert_image_to_pdf is None:
            return jsonify({'error': '图片转换模块不可用'}), 500
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pdf_path = convert_image_to_pdf(src_path, td)
            if not pdf_path or not os.path.exists(pdf_path):
                return jsonify({'error': '图片转 PDF 失败'}), 500
            with open(pdf_path, 'rb') as f:
                data = f.read()
        return Response(data, mimetype='application/pdf',
                        headers={'Content-Disposition': 'inline'})

    # Office / 文本：用现有 convert_to_pdf（LibreOffice）
    try:
        from app import convert_to_pdf
    except ImportError:
        convert_to_pdf = None

    if convert_to_pdf is None:
        return jsonify({'error': '文档转换模块不可用'}), 500

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        pdf_path = convert_to_pdf(src_path, td)
        if not pdf_path or not os.path.exists(pdf_path):
            return jsonify({'error': '文档转 PDF 失败，请确认已安装 LibreOffice'}), 500
        with open(pdf_path, 'rb') as f:
            data = f.read()
    return Response(data, mimetype='application/pdf',
                    headers={'Content-Disposition': 'inline'})


def _is_image_name(name):
    image_ext = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg'}
    _, ext = os.path.splitext(name.lower())
    return ext in image_ext


# 图片 MIME 映射（前端 <img> 显示用）
_IMAGE_MIME = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
}


@preview_bp.route('/api/preview/raw/<token>', methods=['GET'])
def api_preview_raw(token):
    """
    通过 token 直接返回原图（仅图片）。
    PDF/Office 文档请走 /api/preview/pdf/<token>。

    设计：图片预览无需走 img2pdf + pdftoppm 这条慢路径，
    前端拿到 image/* mime 后直接用 <img> 显示即可，避免无谓的 PNG 转换与高清放大。
    """
    with _PREVIEW_LOCK:
        entry = _PREVIEW_CACHE.get(token)
    if not entry:
        return jsonify({'error': 'token 不存在或已过期'}), 404

    src_path = entry['path']
    src_name = entry['original_name']
    if not os.path.exists(src_path):
        return jsonify({'error': '文件已被清理'}), 410

    if not _is_image_name(src_name):
        return jsonify({'error': '该端点仅支持图片，PDF/Office 请用 /api/preview/pdf/<token>'}), 415

    _, ext = os.path.splitext(src_name.lower())
    mime = _IMAGE_MIME.get(ext, 'application/octet-stream')
    return send_file(
        src_path,
        mimetype=mime,
        as_attachment=False,
        conditional=True,  # 支持 Range（浏览器 <img> 大图可能用到）
    )


@preview_bp.route('/api/preview/info', methods=['GET'])
def api_preview_info():
    """诊断端点：返回当前缓存大小、活跃 token 数等（不影响功能）"""
    with _PREVIEW_LOCK:
        tokens = list(_PREVIEW_CACHE.keys())
    return jsonify({
        'cache_count': len(tokens),
        'cache_max': PREVIEW_CACHE_MAX,
        'ttl': PREVIEW_TTL,
        'tokens': [t[:8] + '…' for t in tokens],
    })


# ─── 周期性 GC（防止长期运行时缓存不释放） ────────────────────────────────────
def start_preview_gc(app_obj=None):
    """注册定时 GC 任务（可选，由 app.py 的启动逻辑调用）"""
    import atexit
    _gc_cache()  # 启动时先清一遍
    atexit.register(lambda: _gc_cache())


# 模块加载时跑一次
_gc_cache()