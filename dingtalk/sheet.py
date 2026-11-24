"""生成「打印预览」长图：顶部打印机图标 + 书房打印机标题，下面是待打印内容的前几页。

为什么要生成一张图：
  钉钉卡片的 markdown 不渲染图片（实测 `![]()` 会被丢掉），卡片里的图只能靠
  模板的 image 组件（那是模板的事，改不了）。所以预览做成一张图片消息，
  紧跟在卡片下面发出去，手机上直接就能看到要打的是什么。

排版（宽 750）：
  ┌──────────────────────────────┐
  │ [打印机图标] 书房打印机         │  ← 顶部横幅
  │              打印预览 · 3 张图   │
  ├──────────────────────────────┤
  │  页1      页2                 │  ← 最多 4 页缩略图（2 列）
  │  页3      页4                 │
  ├──────────────────────────────┤
  │ 文件名 / 合并说明               │
  └──────────────────────────────┘
"""
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageOps

LOG = logging.getLogger("bridge.sheet")

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
PRINTER_PNG = os.path.join(ASSETS, "printer.png")

CJK_FONTS = [
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
# Droid Sans Fallback 这套包里的 ASCII 字形是空的（数字/字母会画成方块），
# 所以拉丁字符单独用 DejaVu 画，中文用 Droid —— 见 _draw_text。
LATIN_FONTS = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
]

W = 750
PAD = 20
GAP = 12
BANNER_H = 104
TITLE = "书房打印机"
SUB = "打印预览"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def _font(size: int, bold: bool = False):
    """中文（含全角标点）字体。"""
    for path in CJK_FONTS:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _latin_font(size: int, bold: bool = False):
    """拉丁字符字体（数字、英文字母）。"""
    for bold_path, regular in LATIN_FONTS:
        path = bold_path if bold else regular
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return _font(size, bold)


def _is_ascii(ch: str) -> bool:
    return ord(ch) < 128


def _draw_text(draw, xy, text: str, size: int, fill, bold: bool = False) -> int:
    """混排中英文：中文用 Droid，ASCII 用 DejaVu，返回结束 x 坐标。"""
    f_cjk = _font(size, bold)
    f_lat = _latin_font(size, bold)
    x, y = xy
    run, run_ascii = "", None
    for ch in list(text) + [""]:
        cur = _is_ascii(ch) if ch else None
        if ch and (run_ascii is None or cur == run_ascii):
            run, run_ascii = run + ch, cur
            continue
        if run:
            f = f_lat if run_ascii else f_cjk
            draw.text((x, y), run, font=f, fill=fill)
            x += draw.textlength(run, font=f)
        run, run_ascii = ch, cur
    return int(x)


def _pdf_page_images(path: str, max_pages: int) -> list:
    """取 PDF 的前 max_pages 页，返回 PIL 图列表。"""
    tmp = tempfile.mkdtemp(prefix="sheetpdf-")
    try:
        out = os.path.join(tmp, "p")
        subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", str(max_pages), path, out],
                       capture_output=True, timeout=90)
        imgs = []
        for name in sorted(os.listdir(tmp)):
            if name.endswith(".png"):
                imgs.append(Image.open(os.path.join(tmp, name)).convert("RGB"))
        return imgs
    except Exception as exc:
        LOG.warning("PDF 取页失败 %s: %s", os.path.basename(path), exc)
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pdf_page_count(path: str) -> int:
    """PDF 总页数（pdfinfo），拿不到返回 0。"""
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def count_pages(attachments) -> int:
    """快速统计总页数（PDF 用 pdfinfo，图片算 1 页），不渲染，毫秒级。"""
    total = 0
    for att in attachments:
        ext = os.path.splitext(getattr(att, "path", ""))[1].lower()
        if ext == ".pdf":
            total += pdf_page_count(att.path) or 1
        else:
            total += 1
    return total


def collect_pages(attachments, max_pages: int = 2):
    """收集预览单元，返回 (cells, total_pages)，cells 是 [(PIL 图, 角标文字)]。

    规则（2026-11-25 按反馈改：以前 2 个文件只显示第一个文件的前两页，看不出第二个）：
      - 只有 1 个文件：PDF 取前 max_pages 页
      - 有多个文件：**每个文件各取第 1 页**（最多 max_pages 个），角标写文件名
    """
    cells, total = [], 0
    files = [a for a in attachments if getattr(a, "path", "")]
    multi = len(files) > 1
    for att in files:
        path = att.path
        ext = os.path.splitext(path)[1].lower()
        raw_name = getattr(att, "name", "") or os.path.basename(path)
        name = _short(os.path.basename(raw_name), 18).replace("\u2026", "...") if multi else ""
        if ext == ".pdf":
            pages = pdf_page_count(path) or 1
            total += pages
            if len(cells) >= max_pages:
                continue
            if multi:
                im = _page_image(path, 1200, 1200)          # 第 1 页
                if im is not None:
                    cells.append((im, f"{name}（共 {pages} 页）" if pages > 1 else name))
            else:
                for im in _pdf_page_images(path, max_pages):
                    cells.append((im, ""))
        elif ext in IMAGE_EXT:
            total += 1
            if len(cells) >= max_pages:
                continue
            im = _page_image(path, 1200, 1200)
            if im is not None:
                cells.append((im, name))
        else:
            total += 1
    return cells, total


def _page_image(path: str, max_w: int, max_h: int) -> Image.Image | None:
    """把一张本地文件转成 PIL 图（图片直接读，PDF 取第一页）。"""
    ext = os.path.splitext(path)[1].lower()
    tmp = None
    try:
        if ext in IMAGE_EXT:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)
            return im.convert("RGB")
        if ext == ".pdf":
            tmp = tempfile.mkdtemp(prefix="sheet-")
            out = os.path.join(tmp, "p")
            subprocess.run(["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "1", path, out],
                           capture_output=True, timeout=60)
            for name in sorted(os.listdir(tmp)):
                if name.endswith(".png"):
                    return Image.open(os.path.join(tmp, name)).convert("RGB")
            return None
    except Exception as exc:
        LOG.warning("生成预览失败 %s: %s", os.path.basename(path), exc)
        return None
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


def _fit(im: Image.Image, box_w: int, box_h: int) -> Image.Image:
    im = im.copy()
    im.thumbnail((box_w, box_h), Image.LANCZOS)
    return im


def build_sheet(attachments, out_path: str, max_pages: int = 4) -> bool:
    """attachments: 带 .path/.name 的对象列表。成功写出图片返回 True。"""
    if not attachments:
        return False
    pages = []
    for att in attachments[:max_pages]:
        im = _page_image(att.path, W, 900)
        if im is not None:
            pages.append(im)
    if not pages:
        LOG.info("没有可用预览（%s 个文件都不是图片/PDF）", len(attachments))
        return False

    n = len(pages)
    cols = 1 if n == 1 else 2
    rows = (n + cols - 1) // cols
    cell_w = W - PAD * 2 if cols == 1 else (W - PAD * 2 - GAP) // 2
    # 单页给得高一些，多页按 3:4 左右的格子
    cell_h = 620 if cols == 1 else int(cell_w * 1.34)
    grid_h = rows * cell_h + (rows - 1) * GAP
    foot_txt = _footer(attachments, n)
    foot_h = 44 if foot_txt else 0
    H = BANNER_H + grid_h + foot_h + 24

    canvas = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(canvas)

    # ---------- 顶部横幅 ----------
    try:
        icon = Image.open(PRINTER_PNG).convert("RGBA")
        icon.thumbnail((72, 72), Image.LANCZOS)
        canvas.paste(icon, (PAD + 2, (BANNER_H - icon.height) // 2), icon)
    except Exception as exc:
        LOG.debug("打印机图标缺失: %s", exc)

    tx = PAD + 92
    _draw_text(draw, (tx, 18), TITLE, 34, (28, 30, 33), bold=True)
    _draw_text(draw, (tx + 1, 64), SUB, 20, (140, 146, 153))

    # 分隔线
    draw.line([(PAD, BANNER_H - 1), (W - PAD, BANNER_H - 1)], fill=(232, 234, 237), width=2)

    # ---------- 缩略图网格 ----------
    y0 = BANNER_H
    for idx, im in enumerate(pages):
        r, c = divmod(idx, cols)
        cx = PAD + c * (cell_w + GAP)
        cy = y0 + r * (cell_h + GAP)
        # 淡底 + 边框
        draw.rounded_rectangle([cx, cy, cx + cell_w, cy + cell_h], radius=10,
                               fill=(248, 249, 250), outline=(226, 229, 233), width=2)
        thumb = _fit(im, cell_w - 16, cell_h - 40)
        canvas.paste(thumb, (cx + (cell_w - thumb.width) // 2,
                             cy + 8 + (cell_h - 40 - thumb.height) // 2))
        _draw_text(draw, (cx + 12, cy + cell_h - 26), f"第 {idx + 1} 页", 16, (150, 155, 161))

    # ---------- 底部说明 ----------
    if foot_txt:
        _draw_text(draw, (PAD, y0 + grid_h + 8), foot_txt, 18, (120, 126, 133))

    canvas.save(out_path, "PNG", optimize=True)
    LOG.info("预览长图已生成 %s (%dx%d, %d 页)", os.path.basename(out_path), W, H, n)
    return True


def _footer(attachments, shown: int) -> str:
    total = len(attachments)
    first = os.path.basename(getattr(attachments[0], "name", "") or "")
    if total == 1:
        return f"{_short(first, 40)}，本图仅示意首页"
    more = "" if shown >= total else f"，还有 {total - shown} 个未显示"
    return f"共 {total} 个文件，{_short(first, 26)} 等{more}"


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def build_card_preview(attachments, out_path: str, size=(720, 420), max_pages: int = 2) -> bool:
    """卡片内用的预览图：固定比例（默认 720x420 ≈ 1.71:1）。

    单个 PDF 并排前 2 页；多个文件则每个文件各取第 1 页并标上文件名，
    这样"卡片写 2 个文件、预览只看到一个"的问题就没了。
    """
    if not attachments:
        return False
    W2, H2 = size
    cells, total_pages = collect_pages(attachments, max_pages)
    if not cells:
        return False

    canvas = Image.new("RGB", (W2, H2), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    n = len(cells)
    pad, gap = 24, 12
    has_label = any(lbl for _, lbl in cells)
    label_h = 24 if has_label else 0
    cell_w = (W2 - pad * 2 - gap * (n - 1)) // n
    cell_h = H2 - pad * 2 - 26 - label_h
    for idx, (im, label) in enumerate(cells):
        x = pad + idx * (cell_w + gap)
        draw.rounded_rectangle([x, pad, x + cell_w, pad + cell_h], radius=10,
                               fill=(255, 255, 255), outline=(226, 229, 233), width=2)
        thumb = _fit(im, cell_w - 12, cell_h - 12)
        canvas.paste(thumb, (x + (cell_w - thumb.width) // 2,
                            pad + (cell_h - thumb.height) // 2 + 2))
        if label:
            _draw_text(draw, (x + 2, pad + cell_h + 4), label, 14, (120, 126, 133))

    total_files = len([a for a in attachments if getattr(a, "path", "")]) or len(attachments)
    if total_files > 1:
        note = f"{total_files} 个文件，共 {total_pages} 页，每份取第 1 页"
    elif total_pages > 1:
        note = f"共 {total_pages} 页" + (f"，预览前 {len(cells)} 页" if len(cells) < total_pages else "")
    else:
        note = "第 1 页"

    _draw_text(draw, (pad + 2, H2 - 22), note, 16, (140, 146, 153))
    canvas.save(out_path, "PNG", optimize=True)
    LOG.info("卡片预览图已生成 %s (%dx%d, %d 格)", os.path.basename(out_path), W2, H2, n)
    return True


