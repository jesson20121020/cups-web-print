"""钉钉机器人消息 -> 打印。核心业务逻辑都在这里。

流程：
  1. 收到 picture / file / richText 消息 -> 用 downloadCode 把文件拉回本地
  2. 放进「待打印批次」，静默 BATCH_WINDOW_SECONDS 秒（连发多张图会被合并成一批）
  3. 回一张 markdown 卡片：文件清单 + 当前参数 + 指令
  4. 用户回「1」确认后才真正提交打印（花钱的动作必须二次确认）
"""
import asyncio
import logging
import os
import re
import time
import uuid

import dingtalk_stream
from dingtalk_stream import AckMessage

from . import card_spec
from .card_api import CardAPI
from .store import Settings as Config
from .dingtalk_api import DingTalkAPI
from .media import MediaClient
from .print_client import PrintClient, PrintError
from .sheet import build_card_preview, build_sheet, count_pages

LOG = logging.getLogger("bridge.bot")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
DOC_EXT = {".pdf", ".txt", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".rtf", ".svg"}

PAPERS = ["A4", "A3", "A5", "A6", "B4", "B5", "4x6", "5x7", "3.5x5", "8x10"]

# 卡片静态文案（v8 原生卡片：title/desc/icon 都绑到了模板变量，所以由后端传）
TITLE = "书房打印机"
CARD_DESC = "点一下就能打，TS3380"
CARD_ICON = "https://static.dingtalk.com/media/lALPD0tXvHnK3dXMwMzA_192_192.png"

# v10 卡片（状态卡）用的标题与状态图标（96x96 透明底，自己画的，已传到钉钉 CDN）
CARD_TITLE_V10 = "家庭打印机"
STATUS_ICON = {
    "pending":  "https://static.dingtalk.com/media/lALPD2dDdIGD2R1gYA_96_96.png",   # 灰色打印机
    "printing": "https://static.dingtalk.com/media/lALPM3tCpnNtWB1gYA_96_96.png",   # 蓝色打印机
    "ok":       "https://static.dingtalk.com/media/lALPD0zNidNOlx1gYA_96_96.png",   # 绿色对勾
    "fail":     "https://static.dingtalk.com/media/lALPM25lXrCyCB1gYA_96_96.png",   # 红色叉
    "cancel":   "https://static.dingtalk.com/media/lALPM3pVRackCB1gYA_96_96.png",   # 灰色横杠
}


def _metric_html(label: str, value: str, color: str) -> str:
    """v10 卡片里的「已打印 3 / 6」「整体进度 30%」这类双行富文本。"""
    return ("<font colorTokenV2=common_gray1_color "
            "sizeToken=common_footnote_text_style__font_size>" + label + "</font>"
            "<br><font sizeToken=common_h2_text_style__font_size colorTokenV2=" + color + ">"
            "**" + value + "**</font>")


def _duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    return f"{m}min {s}s" if m else f"{s}s"


def _bar_html(pct: int, width: int = 20) -> str:
    """卡片里没有真正的进度条组件，用方块字符画一条（文本一定会被渲染）。"""
    pct = max(0, min(100, int(pct or 0)))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled) + f"  **{pct}%**"

HELP = (
    "**打印机器人用法**\n\n"
    "1. 把图片/文件发给我（或在群里 @我 加附件），连发多张会合并成一批\n"
    "2. 我回一张待打印卡片，回 `1` 才真正开始打\n"
    "3. 指令：`1` 打印 · `2` 黑白打印 · `3` 单面 · `4` 双面 · `0` 取消\n"
    "4. 自定义：`/p 2份 黑白 单面 A4 1-3`（份数/色彩/单双面/纸张/页码范围，写几个都行）\n"
    "5. 查状态：`/status`"
)


def _ext(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


class Attachment:
    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name

    @property
    def kind(self) -> str:
        ext = _ext(self.name)
        if ext in IMAGE_EXT:
            return "image"
        return "doc"


class Pending:
    def __init__(self, conversation_id: str, conversation_type: str, staff_id: str):
        self.id = uuid.uuid4().hex[:8]
        self.conversation_id = conversation_id
        self.conversation_type = conversation_type
        self.staff_id = staff_id
        self.attachments: list[Attachment] = []
        self.color = ""
        self.duplex = ""
        self.paper = ""
        self.copies = 1
        self.page_range = None
        self.paper_type = ""
        self.created = time.time()
        self.timer: asyncio.Task | None = None
        self.busy = False
        self.data = None          # 原始消息体，兜底回复用
        self.card_id = ""         # 互动卡片 outTrackId
        self.state = "pending"    # pending | printing | done | cancelled | failed
        self.progress = 0
        self.sheet_sent = False   # 预览长图是否已发（每批只发一次）
        self.preview_url = ""     # 卡片内预览图的 CDN 地址（钉钉静态资源）
        self.pages_total = 0      # 总页数（PDF 用 pdfinfo 统计）
        self.curr_file = ""
        self.elapsed = 0
        self.error = ""
        self.sender_nick = ""     # 发消息的人（卡片上「触发用户」）
        self.print_started = 0.0  # 本次打印开始的时间戳（卡片上显示「打印时长」）
        self.server_job_id = ""   # 当前打印任务在 Web 服务里的 job id（用于取消）
        self.cancel_requested = False  # 用户在卡片上点了取消

    @property
    def key(self) -> str:
        return f"{self.conversation_id}:{self.staff_id}"


class PrintBotHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, cfg: Config, api: DingTalkAPI, printer: PrintClient):
        super().__init__()
        self.cfg = cfg
        self.api = api
        self.pc = printer
        self.pending: dict[str, Pending] = {}
        self._bg: set[asyncio.Task] = set()   # 后台任务（ack 之后才干活）
        self._seen: dict[str, float] = {}     # msgId 去重，防钉钉重投导致重复打印
        self._last_hint: dict[str, float] = {}  # 引导语限流（按会话）
        self.card = CardAPI(api)
        self.media = MediaClient(cfg.client_id, cfg.client_secret, cfg.robot_code)
        self._placeholder = ""    # 1x1 透明图（预览图还没准备好时占位）
        self.card_template_id = cfg.card_template_id
        self.card_disabled = False
        self.by_card: dict[str, Pending] = {}  # outTrackId -> Pending

    # ------------------------------------------------------------------ 互动卡片
    def ensure_card_template(self) -> None:
        """启动时准备卡片模板：有 ID 就用，没有就现建一个并缓存到磁盘。"""
        if self.card_template_id:
            LOG.info("使用已配置的卡片模板 %s", self.card_template_id)
            return
        cache = os.path.join(os.path.dirname(self.cfg.inbox_dir), "card_template.txt")
        try:
            if os.path.isfile(cache):
                tid = open(cache, encoding="utf-8").read().strip()
                if tid:
                    self.card_template_id = tid
                    LOG.info("复用缓存卡片模板 %s", tid)
                    return
            tid = self.card.create_template(card_spec.TEMPLATE_NAME, card_spec.schema())
            self.card_template_id = tid
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(tid)
            LOG.info("卡片模板就绪：%s", tid)
        except Exception as exc:
            self.card_disabled = True
            LOG.warning("互动卡片不可用，退回文字卡片：%s", exc)

    def _file_detail(self, pending: Pending) -> str:
        """v8 模板用的「文件」行文案（保留做回滚兼容）。"""
        n = len(pending.attachments)
        pages = pending.pages_total or 0
        if pages > 1:
            return f"{n} 个文件 · 共 {pages} 页"
        return f"{n} 个文件" if n > 1 else "1 个文件"

    def _card_view(self, pending: Pending) -> dict:
        """把当前状态渲染成 v10 卡片参数（状态卡）。

        模板变量（卡片平台 38f3762b…）：
          title          标题
          status_text    状态文案：待确认打印 / 打印中 / 打印成功 / 打印失败 / 已取消
          status_img     状态图标 URL（成功=绿勾、失败=红叉、取消=灰横杠、打印中=蓝、待打印=灰）
          preview_img    顶部横幅：打印预览拼图（多页并排）
          metric1        富文本「已打印 3 / 6」
          metric2        富文本「整体进度 30%」
          who_trigger    触发用户（发消息的人）
          time_trigger   触发时间（收到文件的时间）
          status_time    状态时间（卡片更新时间，MM-DD HH:MM）
          print_duration 打印时长
          files_count    文件数量
          btn_title      按钮文案（一个按钮兼做 打印/取消）
          redirectUrl    按钮跳转（回调模式下不用，留空）
        """
        self._apply_params(pending, {})
        pages = max(0, int(pending.pages_total or 0))
        pct = 0
        done_pages = 0
        icon = STATUS_ICON["pending"]
        color1, color2 = "common_gray1_color", "common_gray1_color"

        if pending.state == "pending":
            status = "打印机离线" if pending.error else "待确认打印"
            btn = "打印机离线" if pending.error else "开始打印"
        elif pending.state == "printing":
            pct = max(0, min(100, int(pending.progress or 0)))
            if pages > 0:
                done_pages = max(0, min(pages, round(pct / 100 * pages)))
            status = f"打印中 {done_pages}/{pages}" if pages else f"打印中 {pct}%"
            icon = STATUS_ICON["printing"]
            btn = f"取消打印({done_pages}/{pages})" if pages else "取消打印"
            color1, color2 = "common_blue1_color", "common_orange1_color"
        elif pending.state == "done":
            pct, done_pages = 100, pages
            status = "打印成功"
            icon = STATUS_ICON["ok"]
            btn = "已打印"
            color1 = color2 = "common_green1_color"
        elif pending.state == "failed":
            status = "打印失败"
            icon = STATUS_ICON["fail"]
            btn = "打印失败"
            color1 = color2 = "common_red1_color"
        else:
            status = "已取消"
            icon = STATUS_ICON["cancel"]
            btn = "已取消"

        if pending.state == "done":
            duration = _duration_text(pending.elapsed)
        elif pending.state == "printing":
            started = pending.print_started or pending.created
            duration = _duration_text(time.time() - started)
        else:
            duration = ""

        preview = pending.preview_url or self._placeholder_url()
        metric1 = _metric_html("已打印", f"{done_pages} / {pages or 1}", color1)
        metric2 = _metric_html("整体进度", f"{pct}%", color2)

        return {
            # ---- v10（当前模板）----
            "title": CARD_TITLE_V10,
            "status_text": status,
            "status_img": icon,
            "preview_img": preview,
            "metric1": metric1,
            "metric2": metric2,
            "who_trigger": getattr(pending, "sender_nick", "") or "家庭成员",
            "time_trigger": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pending.created)),
            "status_time": time.strftime("%m-%d %H:%M"),
            "print_duration": duration,
            "files_count": str(len(pending.attachments)),
            "btn_title": btn,
            "redirectUrl": "",
            # ---- v8/v9（旧模板，留着便于回滚）----
            "desc": CARD_DESC,
            "icon": CARD_ICON,
            "background": getattr(self.cfg, "card_header_bg_url", "") or "",
            "file_detail": self._file_detail(pending),
            "progress": pct,
            "btnPrint": btn,
        }

    def _card_md(self, pending: Pending, meta: str, params: str) -> str:
        """v7 模板兼容用的 markdown（v8 原生卡片不读它，留着方便回滚/降级）。"""
        banner = getattr(self.cfg, "card_banner_url", "") or ""
        preview = pending.preview_url or self._placeholder_url()
        lines = []
        if banner:
            lines += [f"![]({banner})", ""]
        indent = "\u200b\u3000"
        head = meta
        if pending.pages_total > 1 and "共" not in meta:
            head = f"{meta} · 共 {pending.pages_total} 页"
        lines += [f"{indent}**{head}**", ""]
        if preview:
            lines += [f"![]({preview})", ""]
        lines += [indent + params.replace("\n", "\n" + indent)]
        if pending.state == "pending":
            lines += ["", indent + "点「确认打印」才会出纸"]
        elif pending.state == "done":
            lines += ["", indent + "已出纸，记得去取"]
        elif pending.state == "failed":
            lines += ["", indent + (pending.error or "文件没打出来")]
        return "\n".join(lines)

    async def _send_card(self, pending: Pending) -> bool:
        if self.card_disabled or not self.card_template_id:
            return False
        try:
            tid = await asyncio.to_thread(
                self.card.send, self.card_template_id,
                self._card_view(pending),
                conversation_type=pending.conversation_type,
                conversation_id=pending.conversation_id,
                staff_id=pending.staff_id,
                robot_code=self.cfg.robot_code,
            )
            pending.card_id = tid
            self.by_card[tid] = pending
            return True
        except Exception as exc:
            LOG.warning("发互动卡片失败，退回文字：%s", exc)
            return False

    def _placeholder_url(self) -> str:
        """1x1 透明 PNG 的 CDN 地址：预览图没准备好时占位，避免卡片出现破图。"""
        if self._placeholder:
            return self._placeholder
        cache = os.path.join(os.path.dirname(self.cfg.inbox_dir), "placeholder_url.txt")
        try:
            with open(cache, encoding="utf-8") as fh:
                url = fh.read().strip()
            if url:
                self._placeholder = url
                return url
        except OSError:
            pass
        try:
            from PIL import Image
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                tmp = os.path.join(td, "placeholder.png")
                Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(tmp)
                mid = self.media.upload_image(tmp)
            self._placeholder = (f"https://static.dingtalk.com/media/"
                                 f"{mid.lstrip('@')}_8_8.png")
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(self._placeholder)
        except Exception as exc:
            LOG.warning("占位图上传失败：%s", exc)
        return self._placeholder

    async def _refresh_preview(self, pending: Pending) -> None:
        """后台补卡片内预览图，好了再原地更新一次卡片（不拖慢首张卡片）。"""
        if pending.state != "pending":
            return
        await self._prepare_preview(pending)
        if pending.preview_url and pending.card_id:
            await self._say(pending)

    async def _prepare_preview(self, pending: Pending) -> None:
        """生成卡片内预览图并上传到钉钉 CDN，把地址存到 pending.preview_url。

        卡片模板里图片组件的地址绑的是 ${preview}（一个字符串），
        所以这里传的是 https://static.dingtalk.com/... 这种公开地址，
        手机走 4G 也能显示，不依赖家里局域网。
        """
        if not pending.preview_url:
            pending.preview_url = await asyncio.to_thread(self._placeholder_url)
        if not pending.attachments or getattr(pending, "_prev_done", False):
            return
        pending._prev_done = True
        path = os.path.join(self.cfg.inbox_dir, f"prev_{pending.id}.png")
        try:
            ok = await asyncio.to_thread(build_card_preview, pending.attachments, path)
            if not ok:
                return
            mid = await asyncio.to_thread(self.media.upload_image, path)
            # 钉钉静态资源地址必须带 _宽_高.扩展名，否则 404
            pending.preview_url = (f"https://static.dingtalk.com/media/"
                                   f"{mid.lstrip('@')}_720_420.png")
            LOG.info("卡片预览图就绪 %s", pending.preview_url)
        except Exception as exc:
            LOG.warning("卡片预览图生成失败：%s", exc)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def _send_preview(self, pending: Pending) -> None:
        """预览图走「图片消息」：卡片模板里没有图片组件，markdown 也不渲染图片。"""
        if not getattr(self.cfg, "preview_sheet", True) or pending.sheet_sent:
            return
        if not pending.attachments:
            return
        pending.sheet_sent = True
        path = os.path.join(self.cfg.inbox_dir, f"sheet_{pending.id}.png")
        try:
            ok = await asyncio.to_thread(build_sheet, pending.attachments, path)
            if not ok:
                return
            mid = await asyncio.to_thread(self.media.upload_image, path)
            if pending.conversation_type == "2":
                await asyncio.to_thread(self.media.send_image, mid,
                                        open_conversation_id=pending.conversation_id)
            else:
                await asyncio.to_thread(self.media.send_image, mid,
                                        staff_ids=[pending.staff_id])
        except Exception as exc:
            LOG.warning("预览图没发出去：%s", exc)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ------------------------------------------------------------------ 入口
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """必须快速 ack（几秒内），否则钉钉会重投消息 -> 重复打印。
        所以这里只做去重 + 派发，真正的工作丢到后台 task 里。"""
        data = callback.data or {}
        msg_id = data.get("msgId") or ""
        if msg_id:
            now = time.time()
            if msg_id in self._seen:
                LOG.info("重复消息 %s，跳过", msg_id)
                return AckMessage.STATUS_OK, "OK"
            self._seen[msg_id] = now
            for k, ts in list(self._seen.items()):
                if now - ts > 300:
                    self._seen.pop(k, None)

        task = asyncio.create_task(self._dispatch(data))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return AckMessage.STATUS_OK, "OK"

    async def _dispatch(self, data):
        try:
            msgtype = (data.get("msgtype") or "").lower()
            staff_id = data.get("senderStaffId") or data.get("senderId") or ""
            conv_type = str(data.get("conversationType") or "1")
            conv_id = data.get("conversationId") or staff_id

            in_allowed_group = conv_type == "2" and conv_id in self.cfg.allow_group_ids
            if in_allowed_group:
                pass  # 白名单群里的任何人都能触发
            elif self.cfg.allow_user_ids and staff_id not in self.cfg.allow_user_ids:
                LOG.warning("拒绝非白名单用户 %s (%s) 群=%s", staff_id, data.get("senderNick"), conv_id)
                await self._reply(data, f"⛔ 你没有打印权限（staffId `{staff_id}`）。\n管理员可把它加进 `ALLOW_USER_IDS`。")
                return

            LOG.info("收到消息 type=%s from=%s(%s) conv=%s/%s(%s)", msgtype, data.get("senderNick"),
                     staff_id, conv_type, conv_id, data.get("conversationTitle") or "")

            if msgtype in ("picture", "file", "richtext"):
                await self._on_attachment(data, conv_id, conv_type, staff_id)
            elif msgtype == "text":
                text = ((data.get("text") or {}).get("content") or "").strip()
                await self._on_text(data, text, conv_id, conv_type, staff_id)
            else:
                await self._reply(data, f"暂不支持「{msgtype}」类型消息，发图片或文件给我就行。")
        except Exception as exc:
            LOG.exception("处理消息失败")
            try:
                await self._reply(data, f"❌ 处理失败：{exc}")
            except Exception:
                pass

    # ------------------------------------------------------------------ 附件
    async def _on_attachment(self, data, conv_id, conv_type, staff_id):
        try:
            items = self._extract_attachments(data)
        except Exception as exc:
            LOG.exception("解析附件失败")
            await self._reply(data, f"❌ 解析附件失败：{exc}")
            return
        if not items:
            await self._reply(data, "没在这条消息里找到可打印的图片/文件，试试单独发图片或文件。")
            return

        key = f"{conv_id}:{staff_id}"
        pending = self._batch_for(key, conv_id, conv_type, staff_id)
        pending.data = data
        nick = (data.get("senderNick") or "").strip()
        if nick and not pending.sender_nick:
            pending.sender_nick = nick

        saved = []
        for item in items:
            try:
                path = await asyncio.to_thread(
                    self.api.download, item["download_code"], self._dest_path(conv_id, item["name"])
                )
                pending.attachments.append(Attachment(path, item["name"]))
                saved.append(item["name"])
            except Exception as exc:
                LOG.exception("下载附件失败 %s", item.get("name"))
                await self._reply(data, f"❌ 下载「{item.get('name')}」失败：{exc}")

        if not saved:
            return

        # 静默窗口：连发多张图合并成一批，只回一张卡片
        if pending.timer and not pending.timer.done():
            pending.timer.cancel()
        pending.timer = asyncio.create_task(self._flush_later(pending, data))

    def _batch_for(self, key: str, conv_id: str, conv_type: str, staff_id: str) -> Pending:
        """取「当前正在攒的批次」；已出过卡片的批次视为已封口，新文件另起一批。

        规则（2026-11-25 用户要求）：一张卡片只对应「上一张卡片之后到达的文件」。
        以前是同一个 pending 一直往里塞，于是第二张卡片会把上一张卡片的文件也带上。
        """
        pending = self.pending.get(key)
        if pending is not None:
            expired = (time.time() - pending.created) > self.cfg.pending_ttl * 60
            sealed = bool(pending.card_id) or pending.state != "pending" or pending.busy
            if expired or sealed:
                self._drop_pending(pending)
                pending = None
        if pending is None:
            pending = Pending(conv_id, conv_type, staff_id)
            self.pending[key] = pending
            LOG.info("新批次开始 key=%s", key)
        return pending

    def _drop_pending(self, pending: Pending) -> None:
        """只弃掉这个批次自己；别把同一会话里新开的批次一起删了。"""
        if self.pending.get(pending.key) is pending:
            self.pending.pop(pending.key, None)

    def _dest_path(self, conv_id: str, name: str) -> str:
        safe = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", name or "file")[:80]
        folder = os.path.join(self.cfg.inbox_dir, re.sub(r"[^\w\-]+", "_", conv_id)[:40])
        return os.path.join(folder, f"{int(time.time() * 1000)}_{safe}")

    def _extract_attachments(self, data) -> list:
        msgtype = (data.get("msgtype") or "").lower()
        content = data.get("content") or {}
        if isinstance(content, str):
            import json
            try:
                content = json.loads(content)
            except Exception:
                content = {}

        out = []
        if msgtype == "picture":
            code = content.get("downloadCode") or content.get("pictureDownloadCode")
            if code:
                out.append({"download_code": code, "name": f"dingtalk_{int(time.time())}.jpg"})
        elif msgtype == "file":
            code = content.get("downloadCode")
            if code:
                out.append({"download_code": code, "name": content.get("fileName") or f"file_{int(time.time())}"})
        elif msgtype == "richtext":
            for item in (data.get("content") or {}).get("richText") or []:
                if isinstance(item, dict) and item.get("downloadCode") and (item.get("type") or "").lower() in ("picture", "file"):
                    name = item.get("fileName") or (
                        f"richtext_{int(time.time())}.jpg" if item.get("type") == "picture" else f"richtext_{int(time.time())}"
                    )
                    out.append({"download_code": item["downloadCode"], "name": name})
        return out

    async def _flush_later(self, pending: Pending, data):
        # 防抖而不是死等：最后一个附件到达后静默 1.5s 就出卡片；
        # 最多等 batch_window 秒（连发多张图仍会合并成一批）
        window = float(self.cfg.batch_window or 8)
        quiet = min(1.5, window)
        deadline = time.time() + window
        last_n, last_change = len(pending.attachments), time.time()
        while time.time() < deadline:
            await asyncio.sleep(0.3)
            n = len(pending.attachments)
            if n != last_n:
                last_n, last_change = n, time.time()
            elif time.time() - last_change >= quiet:
                break
        if not pending.attachments:
            return
        try:
            pending.pages_total = await asyncio.to_thread(count_pages, pending.attachments)
        except Exception as exc:
            LOG.warning("统计页数失败：%s", exc)
        if await self._send_card(pending):
            LOG.info("已发出互动卡片（单按钮）")
            task = asyncio.create_task(self._refresh_preview(pending))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            task2 = asyncio.create_task(self._send_preview(pending))
            self._bg.add(task2)
            task2.add_done_callback(self._bg.discard)
        else:
            await self._reply(data, self._card(pending))
        self._gc()

    # ------------------------------------------------------------------ 文本
    async def _on_text(self, data, text, conv_id, conv_type, staff_id):
        key = f"{conv_id}:{staff_id}"
        pending = self.pending.get(key)
        if pending and (time.time() - pending.created) > self.cfg.pending_ttl * 60:
            self._drop_pending(pending)
            pending = None

        low = text.lower().strip()

        if low in ("/help", "help", "帮助", "?", "？"):
            await self._reply(data, HELP)
            return

        if low in ("/status", "状态", "打印机", "printer"):
            await self._reply(data, await asyncio.to_thread(self._status_text))
            return

        # 自定义参数：/p 2份 黑白 单面 4x6
        if low.startswith("/p") or text.startswith("打印"):
            override = self._parse_params(text)
            if pending is None:
                await self._reply(data, "现在没有待打印的文件。先把图片/文件发给我，再回 `1` 或 `/p ……`。")
                return
            self._apply_params(pending, override)
            await self._do_print(data, pending)
            return

        if not pending:
            await self._hint_no_file(data, conv_id, conv_type, staff_id)
            return

        if text in ("0", "取消", "cancel"):
            self._drop_pending(pending)
            await self._reply(data, "已取消，文件不会打印。")
            return

        if text in ("1", "打印", "确认", "ok", "OK"):
            await self._do_print(data, pending)
            return
        if text == "2":
            pending.color = "mono"
            await self._do_print(data, pending)
            return
        if text == "3":
            pending.duplex = "one-sided"
            await self._do_print(data, pending)
            return
        if text == "4":
            pending.duplex = "two-sided-long-edge"
            await self._do_print(data, pending)
            return

        override = self._parse_params(text)
        if override:
            self._apply_params(pending, override)
            await self._do_print(data, pending)
            return

        await self._reply(data, "没看懂。回 `1` 打印 / `0` 取消，或 `/help` 看用法。")

    async def _hint_no_file(self, data, conv_id: str, conv_type: str, staff_id: str):
        """没有待打印文件时的引导。同一个会话 3 分钟内只回一次，避免群里刷屏。"""
        now = time.time()
        if now - self._last_hint.get(conv_id, 0) < 180:
            LOG.info("引导语限流（%s），跳过回复", conv_id)
            return
        self._last_hint[conv_id] = now
        if conv_type == "2":
            msg = ("群里 @我 只会把**文字**推给我，图片/文件是单独一条消息，钉钉不会转发给机器人。\n\n"
                   "想在这里打，可以：把图片和 @我 放在**同一条消息**里发（PC 端粘贴图片最容易）；\n"
                   "或者更省事——长按图片转发/保存，然后发到我的**单聊**，那边一步到位。")
        else:
            msg = "还没有待打印的东西。把图片或文件发我就行，`/help` 看用法。"
        await self._reply(data, msg)

    def _parse_params(self, text: str) -> dict:
        """从一句话里抠打印参数，抠到几个算几个。"""
        out = {}
        t = text.strip()
        if t.lower().startswith("/p"):
            t = t[2:]

        m = re.search(r"(\d{1,2})\s*(份|copy|copies)", t, re.I)
        if m:
            out["copies"] = max(1, min(99, int(m.group(1))))
        elif re.fullmatch(r"\s*[1-9]\d?\s*", t):  # 单独一个数字 = 份数（0 留给取消）
            out["copies"] = max(1, min(99, int(t.strip())))

        if re.search(r"黑白|单色|mono|省墨", t, re.I):
            out["color"] = "mono"
        elif re.search(r"彩色|color", t, re.I):
            out["color"] = "color"

        if re.search(r"单面|simplex|单页", t, re.I):
            out["duplex"] = "one-sided"
        elif re.search(r"双面|duplex", t, re.I):
            out["duplex"] = "two-sided-long-edge"

        for p in PAPERS:
            if re.search(rf"(?<![0-9A-Za-z]){re.escape(p)}(?![0-9A-Za-z])", t, re.I):
                out["paper"] = p
                break

        if re.search(r"照片|相纸|photo", t, re.I):
            out["paper_type"] = "photo"

        # 页码范围：先认「第2-3页 / 2-3 页」这种写在页前面的，再认「页:2-3」
        m = re.search(r"第?\s*(\d{1,3}(?:\s*[-~,，]\s*\d{1,3})*)\s*页", t)
        if m and re.search(r"[-~,，]", m.group(1)):
            out["page_range"] = re.sub(r"\s+", "", m.group(1)).replace("~", "-").replace("，", ",")
        else:
            m = re.search(r"(?:页码|pages?)\s*[:：]?\s*([0-9,\-]+)", t, re.I)
            if m:
                out["page_range"] = re.sub(r"\s+", "", m.group(1))
            else:
                # 没写「页」也要认 1-3 / 1,3,5 这种带分隔符的页码范围；纯数字不认（可能是份数）
                m = re.search(r"(?<![\w.])(\d{1,3}(?:\s*[-~]\s*\d{1,3})?(?:\s*[,，]\s*\d{1,3}(?:\s*[-~]\s*\d{1,3})?)+|\d{1,3}\s*[-~]\s*\d{1,3})(?![\w.])", t)
                if m:
                    rng = re.sub(r"\s+", "", m.group(1)).replace("~", "-").replace("，", ",")
                    if re.fullmatch(r"[0-9\-,]+", rng):
                        out["page_range"] = rng
        return out

    def _apply_params(self, pending: Pending, override: dict):
        cfg = self.cfg
        pending.color = override.get("color", pending.color or cfg.default_color)
        pending.duplex = override.get("duplex", pending.duplex or cfg.default_duplex)
        pending.paper = override.get("paper", pending.paper or cfg.default_paper)
        pending.paper_type = override.get("paper_type", pending.paper_type or cfg.default_paper_type)
        if "copies" in override:
            pending.copies = override["copies"]
        if "page_range" in override:
            pending.page_range = override["page_range"]

    # ------------------------------------------------------------------ 打印
    async def _do_print(self, data, pending: Pending):
        if pending.busy or pending.state == "printing":
            await self._say(pending, "上一批还在处理，稍等。")
            return
        if not pending.attachments:
            self._drop_pending(pending)
            await self._say(pending, "待打印队列是空的。")
            return
        pending.busy = True
        pending.state = "printing"
        pending.print_started = time.time()
        key = pending.key
        self._apply_params(pending, {})
        started = time.time()

        try:
            health = await asyncio.to_thread(self.pc.health)
            if not health["online"]:
                pending.state = "pending"
                pending.error = f"打印机离线：{health['reason']}"
                await self._say(pending)
                return

            printer = health["printer"]
            results = []
            for job in self._plan_jobs(pending):
                name, path = job["name"], job["path"]
                if pending.cancel_requested:
                    LOG.info("用户取消，跳过剩余任务：%s", name)
                    break
                try:
                    server_name = await asyncio.to_thread(self.pc.upload, path)
                    resp = await asyncio.to_thread(
                        self.pc.submit, server_name,
                        printer=printer, color=pending.color, duplex=pending.duplex,
                        paper=pending.paper, paper_type=pending.paper_type,
                        copies=pending.copies, page_range=pending.page_range,
                        scaling=self.cfg.default_scaling,
                    )
                    pending.server_job_id = str(resp.get("job_id") or "")
                    job_info = await self._wait_job(pending, resp["job_id"], name)
                    results.append((name, resp.get("job_id"), job_info))
                except PrintError as exc:
                    results.append((name, None, {"status": "failed", "message": str(exc)}))
                except Exception as exc:
                    LOG.exception("打印失败")
                    results.append((name, None, {"status": "failed", "message": str(exc)}))

            elapsed = int(time.time() - started)
            statuses = [(i.get("status") or "").lower() for _, _, i in results]
            ok = bool(statuses) and all(s == "completed" for s in statuses)
            cancelled = pending.cancel_requested or any("cancel" in s for s in statuses)
            if ok:
                pending.state = "done"
            elif cancelled:
                pending.state = "cancelled"
                pending.error = "已取消打印"
            else:
                pending.state = "failed"
            pending.elapsed = elapsed
            await self._say(pending)
            LOG.info("打印结果 %s：%s", printer, "; ".join(
                f"{n}={i.get('status')}" for n, _, i in results))
            self._drop_pending(pending)
            self.by_card.pop(pending.card_id, None)
            self._cleanup_files(pending)
        finally:
            pending.busy = False

    async def _wait_job(self, pending: Pending, job_id: str, name: str) -> dict:
        """轮询任务状态，顺便把进度刷到卡片上（最多每 3 秒、进度变化 >=5% 才刷）。"""
        deadline = time.time() + self.cfg.job_timeout
        last = {}
        last_pct = -1
        last_ts = 0.0
        while time.time() < deadline:
            if pending.cancel_requested:
                LOG.info("用户取消，停止等待 job=%s", job_id)
                return {"status": "cancelled", "message": "用户取消打印"}
            try:
                last = await asyncio.to_thread(self.pc.job, job_id) or last
            except Exception as exc:
                LOG.warning("查询 job 失败: %s", exc)
            status = (last.get("status") or "").lower()
            if status in ("completed", "failed", "cancelled", "canceled"):
                return last
            pct = int(last.get("progress") or 0)
            now = time.time()
            if now - last_ts >= 3 and (pct - last_pct >= 5 or pct >= 95):
                last_pct, last_ts = pct, now
                pending.curr_file = name
                pending.progress = pct
                await self._say(pending)
            await asyncio.sleep(2)
        last = dict(last or {})
        last["status"] = "timeout"
        return last
    # ---------- 卡片文案 ----------
    def _params_line(self, pending: Pending) -> str:
        self._apply_params(pending, {})
        line = (f"{pending.paper} · {'彩色' if pending.color == 'color' else '黑白'} · "
                f"{'单面' if pending.duplex == 'one-sided' else '双面长边'} · {pending.copies} 份")
        if pending.page_range:
            line += f" · 第 {pending.page_range} 页"
        return line

    def _plain_text(self, pending: Pending) -> str:
        """卡片不可用时的文字版兜底。"""
        v = self._card_view(pending)
        lines = [v["title"], v["desc"], "", v["file_detail"]]
        if pending.state == "printing":
            lines.append(f"打印进度 {v['progress']}%")
        elif pending.state == "pending":
            lines.append("回 1 打印 · 0 取消｜/p 改参数 · /status 看状态")
        elif pending.state == "done":
            lines.append("已出纸，记得去取")
        elif pending.state == "failed":
            lines.append(pending.error or "文件没打出来")
        return "\n".join(lines)

    async def _say(self, pending: Pending, note: str = ""):
        """优先原地更新互动卡片；卡片不可用时退回文字消息。"""
        if note:                      # 插话（如"上一批还在处理"）：单独回文字，不动卡片
            if pending.data:
                await self._reply(pending.data, note[:200])
            return
        view = self._card_view(pending)
        if pending.card_id and not self.card_disabled:
            ok = await asyncio.to_thread(self.card.update, pending.card_id, view)
            if ok:
                return
        if pending.data:
            await self._reply(pending.data, self._plain_text(pending))

    async def on_card_action(self, out_track_id: str, action_id: str, user_id: str):
        pending = self.by_card.get(out_track_id)
        LOG.info("卡片点击 outTrackId=%s action=%s user=%s", out_track_id, action_id, user_id)

        if pending is None:
            await asyncio.to_thread(
                self.card.update, out_track_id,
                {"file_detail": "这张卡片已经处理过了", "btnPrint": "已处理"})
            return
        if pending.state in ("done", "failed", "cancelled") and action_id != "print_again":
            LOG.info("卡片已处于 %s，忽略重复点击", pending.state)
            return

        if self.cfg.allow_user_ids and user_id and user_id not in self.cfg.allow_user_ids:
            # 卡片回调里的 userId 可能是加密过的 staffId，对不上就只记日志不拦
            LOG.warning("卡片点击者 %s 不在白名单（仅记录）", user_id)

        if action_id == "cancel":
            self._drop_pending(pending)
            self.by_card.pop(out_track_id, None)
            pending.state = "cancelled"
            pending.error = "这些文件不会打印"
            self._cleanup_files(pending)
            await self._say(pending)
            return

        # 打印中再点按钮 = 取消打印（一个按钮兼做打印/取消）
        if pending.state == "printing":
            if pending.cancel_requested:
                await self._say(pending)
                return
            pending.cancel_requested = True
            LOG.info("卡片点击取消打印 job=%s", pending.server_job_id)
            # 不用立刻刷卡片：_wait_job 会马上返回 cancelled，_do_print 收尾时把卡片
            # 一次性刷成「已取消」，避免闪一下又变回"打印中"
            if pending.server_job_id:
                await asyncio.to_thread(self.pc.cancel, pending.server_job_id)
            return

        # confirm（默认按确认处理）
        await self._do_print(None, pending)

    def _plan_jobs(self, pending: Pending) -> list:
        """多张图片合并成一个 PDF 再打；混合类型/单个文件则逐个打印。"""
        atts = pending.attachments
        if len(atts) > 1 and all(a.kind == "image" for a in atts):
            merged = self._merge_images(atts)
            if merged:
                return [{"name": f"合并_{len(atts)}张图.pdf", "path": merged}]
        return [{"name": a.name, "path": a.path} for a in atts]

    def _merge_images(self, atts: list) -> str | None:
        try:
            import img2pdf  # 服务端镜像里也有，本地装的这个只是方便合并
            out = os.path.join(os.path.dirname(atts[0].path), f"merged_{int(time.time())}.pdf")
            with open(out, "wb") as fh:
                fh.write(img2pdf.convert([a.path for a in atts]))
            LOG.info("已合并 %d 张图为 %s", len(atts), out)
            return out
        except Exception as exc:
            LOG.warning("图片合并失败，改为逐个打印：%s", exc)
            return None

    def _result_text(self, pending: Pending, results: list, printer: str) -> str:
        params = f"{pending.paper} · {'彩色' if pending.color == 'color' else '黑白'} · " \
                 f"{'单面' if pending.duplex == 'one-sided' else '双面'} · {pending.copies} 份"
        lines = [f"**打印结果**（{params}）", ""]
        for name, job_id, info in results:
            status = (info.get("status") or "unknown").lower()
            msg = (info.get("message") or "").strip()
            if status == "completed":
                lines.append(f"✅ {name} — 已打完" + (f"（job {job_id}）" if job_id else ""))
            elif status == "timeout":
                lines.append(f"⏳ {name} — 已提交，还在队列里（job {job_id}），可以用 `/status` 跟进")
            elif status == "cancelled" or status == "canceled":
                lines.append(f"⚪ {name} — 已取消")
            else:
                detail = f"：{msg}" if msg else ""
                lines.append(f"❌ {name} — 失败{detail}")
        if any((i.get("status") or "").lower() == "failed" for _, _, i in results):
            lines.append("")
            lines.append("如果原因是「USB 未被系统识别」，NAS 上的看门狗会在 1–2 分钟内自动复位，之后回 `1` 重试即可。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 卡片/工具
    def _card(self, pending: Pending) -> str:
        self._apply_params(pending, {})
        names = "\n".join(
            f"{i + 1}. {a.name}（{_human(os.path.getsize(a.path)) if os.path.exists(a.path) else '?'}）"
            for i, a in enumerate(pending.attachments)
        )
        merged = len(pending.attachments) > 1 and all(a.kind == "image" for a in pending.attachments)
        params = f"{pending.paper} · {'彩色' if pending.color == 'color' else '黑白'} · " \
                 f"{'单面' if pending.duplex == 'one-sided' else '双面长边'} · {pending.copies} 份"
        if pending.page_range:
            params += f" · 第 {pending.page_range} 页"
        tip = "\n> 多张图片会合并成一个 PDF 一起打" if merged else ""
        return (
            f"**📄 待打印（{len(pending.attachments)} 个文件）**\n\n{names}\n\n"
            f"**参数**：{params}{tip}\n\n"
            "回 `1` 打印 ｜ `2` 黑白 ｜ `3` 单面 ｜ `4` 双面 ｜ `0` 取消\n"
            "自定义：`/p 2份 黑白 单面 A4 1-3`"
        )

    def _status_text(self) -> str:
        try:
            printer = self.pc.resolve_printer()
            info = self.pc.probe(printer)
        except Exception as exc:
            return f"❌ 连不上打印服务：{exc}"
        online = info.get("online_status")
        icon = {"online": "🟢", "offline": "🔴"}.get(online, "🟡")
        text = f"{icon} 打印机 `{printer}`：{online} / {info.get('status')}"
        if info.get("offline_reason"):
            text += f"\n原因：{info['offline_reason']}"
        try:
            jobs = [j for j in self.pc._session.get(f"{self.pc.base}/api/jobs", timeout=15).json().get("jobs", [])
                    if (j.get("status") or "").lower() not in ("completed", "failed", "cancelled", "canceled")]
        except Exception:
            jobs = []
        if jobs:
            text += "\n\n**队列中**："
            for j in jobs[:5]:
                text += f"\n· {j.get('filename', '?')} — {j.get('status')} {j.get('message') or ''}"
        return text

    async def _reply(self, data, markdown: str):
        LOG.info("回复 -> %s", markdown.replace("\n", " / ")[:200])
        try:
            msg = dingtalk_stream.ChatbotMessage.from_dict(data)
            await asyncio.to_thread(self.reply_markdown, "家里打印机", markdown, msg)
        except Exception:
            LOG.exception("回复消息失败，改走 sessionWebhook")
            try:
                hook = data.get("sessionWebhook")
                if hook:
                    import requests
                    await asyncio.to_thread(
                        requests.post, hook,
                        json={"msgtype": "markdown", "markdown": {"title": "家里打印机", "text": markdown}},
                        timeout=15,
                    )
            except Exception:
                LOG.exception("sessionWebhook 兜底也失败")

    def _cleanup_files(self, pending: Pending):
        for a in pending.attachments:
            try:
                os.unlink(a.path)
            except OSError:
                pass

    def _gc(self):
        now = time.time()
        ttl = self.cfg.pending_ttl * 60
        for key, p in list(self.pending.items()):
            if now - p.created > ttl:
                self.pending.pop(key, None)
                self._cleanup_files(p)
        # 出过卡片但一直没点的老批次（已不在 self.pending 里）也要回收，别让文件一直躺着
        for tid, p in list(self.by_card.items()):
            if p.state == "pending" and not p.busy and (now - p.created) > ttl:
                self.by_card.pop(tid, None)
                self._cleanup_files(p)
                LOG.info("回收超时未打印的卡片批次 %s", tid)
