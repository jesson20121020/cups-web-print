"""从钉钉群里自动抓作业：用官方 dws CLI 以用户身份读群消息，把图片/文件拉下来出打印卡片。

依赖：
  - dws CLI（官方开源，用户已 device-flow 登录）
  - 组织已开启「其他渠道 CLI 访问」权限

流程：
  dws chat +chat-messages --group <群名>   →  找出新消息里的 resourceRefs
  dws chat +messages-resource-download     →  下载到本地
  PrintBotHandler._send_card()             →  发互动卡片（打印/取消）
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time

from .bot import Attachment, Pending

LOG = logging.getLogger("bridge.group")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MAX_BYTES = 100 * 1024 * 1024


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class GroupWatcher:
    def __init__(self, cfg, handler):
        self.cfg = cfg
        self.bot = handler
        # 放在挂载卷里，容器重建后仍记得哪些消息处理过
        self.seen_path = os.path.join(cfg.inbox_dir, "group_seen.json")
        self.seen = self._load_seen()
        self.retry: dict[str, int] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ev_loop = None
        self.stop = False

    # ---------- 状态 ----------
    def _load_seen(self) -> dict:
        try:
            with open(self.seen_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_seen(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.seen_path), exist_ok=True)
            cutoff = time.time() - 7 * 86400
            self.seen = {k: v for k, v in self.seen.items() if v > cutoff}
            with open(self.seen_path, "w", encoding="utf-8") as fh:
                json.dump(self.seen, fh)
        except Exception as exc:
            LOG.warning("保存已读状态失败：%s", exc)

    # ---------- dws 调用 ----------
    def _dws(self, args: list, cwd: str = None, timeout: int = 90) -> dict:
        cmd = [self.cfg.dws_bin] + args
        env = dict(os.environ)
        # 必须指向应用自己的 CLI 数据目录，否则会去读 ~/.dws（无权限/无登录态）
        env["DWS_CONFIG_DIR"] = getattr(self.cfg, "dws_config_dir", "")
        env["DWS_KEYCHAIN_DIR"] = getattr(self.cfg, "dws_keychain_dir", "")
        env["DWS_NO_UPDATE_CHECK"] = "1"
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"dws 超时：{' '.join(args[:3])}")
        if out.returncode != 0:
            raise RuntimeError(f"dws 失败({out.returncode})：{(out.stderr or out.stdout)[:300]}")
        text = out.stdout.strip()
        start = text.find("{")
        if start < 0:
            raise RuntimeError(f"dws 返回无法解析：{text[:200]}")
        return json.loads(text[start:])

    def mget(self, msg_ids: list) -> list:
        """按消息 ID 批量取详情（含 resourceRefs）。一次最多 50 条。"""
        out = []
        for i in range(0, len(msg_ids), 50):
            batch = msg_ids[i:i + 50]
            data = self._dws(["chat", "+messages-mget", "--msg-ids", ",".join(batch), "-f", "json"])
            out.extend(data.get("messages") or [])
        return out

    def fetch_messages(self) -> list:
        data = self._dws(["chat", "+chat-messages", "--group", self.cfg.group_name,
                          "--limit", str(self.cfg.group_fetch_limit), "-f", "json"])
        return data.get("messages") or []

    # ---------- 下载 ----------
    def download(self, ref: dict, dest_name: str) -> str:
        args = ref.get("download", {}).get("arguments") or {}
        if not args or not args.get("resource-id"):
            raise RuntimeError("资源缺少下载参数")
        work = os.path.join(self.cfg.inbox_dir, "group_dl")
        os.makedirs(work, exist_ok=True)
        cmd = ["chat", "+messages-resource-download",
               "--resource-id", args["resource-id"],
               "--type", args.get("type") or ref.get("type") or "mediaId",
               "--output", dest_name, "--overwrite", "-f", "json"]
        # mediaId 需要消息身份；fileId 可独立下载（钉钉返回里就没有这两个字段）
        if args.get("message-id"):
            cmd += ["--message-id", args["message-id"]]
        if args.get("open-conversation-id"):
            cmd += ["--open-conversation-id", args["open-conversation-id"]]
        self._dws(cmd, cwd=work)
        rel = dest_name
        path = os.path.join(work, rel)
        if not os.path.isfile(path):
            raise RuntimeError("下载后文件不存在")
        if os.path.getsize(path) > MAX_BYTES:
            os.unlink(path)
            raise RuntimeError("文件超过 100MB")
        return path

    @staticmethod
    def _pick_name(msg: dict, ref: dict, idx: int = 0) -> str:
        raw = (ref.get("name") or "").strip()
        if raw:
            safe = re.sub(r"[\\/:*?\"<>|]+", "_", raw)[:80]
            stem, ext = os.path.splitext(safe)
            if not ext:
                ext = ".pdf" if (ref.get("contentType") or "") == "file" else ""
            stamp = msg.get("createTime", "").replace(":", "").replace(" ", "_").replace("-", "")
            return f"{stamp}_{stem}{ext}"
        ctype = (ref.get("contentType") or "").lower()
        ext = ".jpg"
        if ctype and ctype not in ("image",):
            ext = "." + re.sub(r"[^a-z0-9]", "", ctype)[:5] or ".bin"
        mid = re.sub(r"[^A-Za-z0-9]", "", msg.get("messageId", ""))[:12]
        rid = re.sub(r"[^A-Za-z0-9]", "", ref.get("resourceId", ""))[-8:]
        stamp = msg.get("createTime", "").replace(":", "").replace(" ", "_").replace("-", "")
        return f"{stamp}_{mid}_{idx}_{rid}{ext}"

    # ---------- 主循环 ----------
    def bind_loop(self, loop) -> None:
        self._ev_loop = loop

    def submit(self, msg_ids: list) -> None:
        """线程安全地把"有新消息"塞进处理队列（给事件中继用）。"""
        if not self._ev_loop or not msg_ids:
            return
        self._ev_loop.call_soon_threadsafe(self._queue.put_nowait, list(msg_ids))

    async def _consume(self) -> None:
        LOG.info("事件消费者已就绪")
        while not self.stop:
            ids = await self._queue.get()
            LOG.info("消费者取到 %d 条消息", len(ids))
            try:
                msgs = await asyncio.to_thread(self.mget, ids)
            except Exception as exc:
                LOG.warning("取消息详情失败：%s", exc)
                continue
            for m in msgs:
                if m.get("messageId") not in self.seen:
                    await self._handle(m)

    async def run(self) -> None:
        LOG.info("群监听启动：组群「%s」，每 %ds 轮询一次", self.cfg.group_name, self.cfg.group_poll_seconds)
        # 首次把已有消息标为已读，避免一启动就把历史全打出来
        asyncio.create_task(self._consume())
        if not self.cfg.group_skip_history:
            LOG.info("GROUP_SKIP_HISTORY=0：启动时也处理最近消息")
            return await self._loop()
        try:
            for m in await asyncio.to_thread(self.fetch_messages):
                self.seen[m.get("messageId", "")] = time.time()
            await asyncio.to_thread(self._save_seen)
            LOG.info("已跳过启动前的历史消息（%d 条）", len(self.seen))
        except Exception as exc:
            LOG.warning("首次拉取失败（稍后继续）：%s", exc)

        await self._loop()

    async def _loop(self) -> None:
        while not self.stop:
            try:
                msgs = await asyncio.to_thread(self.fetch_messages)
                fresh = [m for m in msgs if m.get("messageId") and m["messageId"] not in self.seen]
                for m in reversed(fresh):          # 从旧到新，保持打印顺序
                    await self._handle(m)
            except Exception as exc:
                LOG.warning("轮询群消息失败：%s", exc)
            await asyncio.sleep(self.cfg.group_poll_seconds)

    async def _handle(self, msg: dict) -> None:
        mid = msg["messageId"]
        refs = msg.get("resourceRefs") or []
        if not refs:
            self.seen[mid] = time.time()
            await asyncio.to_thread(self._save_seen)
            return
        LOG.info("群里新作业：%s @%s 资源 %d 个", msg.get("createTime"), msg.get("sender"), len(refs))

        atts = []
        seen_hash = {}
        for idx, ref in enumerate(refs):
            try:
                path = await asyncio.to_thread(self.download, ref, self._pick_name(msg, ref, idx))
            except Exception as exc:
                LOG.warning("下载失败：%s", exc)
                continue
            # 同一条消息里，钉钉常给同一张图两个 ref（原图 + 同一资源），按内容去重
            digest = await asyncio.to_thread(_sha256, path)
            if digest in seen_hash:
                LOG.info("跳过重复资源（与第 %d 个内容相同）：%s", seen_hash[digest] + 1, os.path.basename(path))
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            seen_hash[digest] = idx
            atts.append(Attachment(path, os.path.basename(path)))
        if not atts:
            tries = self.retry.get(mid, 0) + 1
            self.retry[mid] = tries
            if tries >= 3:
                self.seen[mid] = time.time()
                LOG.warning("连续 %d 次下载失败，跳过消息 %s", tries, mid)
            await asyncio.to_thread(self._save_seen)
            return
        self.seen[mid] = time.time()
        await asyncio.to_thread(self._save_seen)

        target_staff = self.cfg.card_target_staff_id
        pending = Pending(
            conversation_id=msg.get("conversationId", ""),
            conversation_type="1" if target_staff else "2",
            staff_id=target_staff or msg.get("senderId", ""),
        )
        pending.attachments = atts
        pending.data = None
        key = f"group:{mid}"
        self.bot.pending[key] = pending
        if await self.bot._send_card(pending):
            LOG.info("已为群里新作业发出打印卡片（%d 个文件）", len(atts))
        else:
            LOG.warning("卡片发送失败，文件保留在 %s", os.path.dirname(atts[0].path) if atts else "-")


class GroupEventWatcher(GroupWatcher):
    """事件触发版：常驻订阅「指定群消息」事件流，来一条处理一条；定时轮询只做兜底。"""

    async def run(self) -> None:
        LOG.info("事件监听启动：群 %s（兜底轮询 %ds）", self.cfg.group_name, self.cfg.group_catchup_seconds)
        asyncio.create_task(self._catchup())
        while not self.stop:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("事件流异常，5 秒后重连：%s", exc)
            await asyncio.sleep(5)

    async def _catchup(self) -> None:
        while not self.stop:
            await asyncio.sleep(self.cfg.group_catchup_seconds)
            try:
                msgs = await asyncio.to_thread(self.fetch_messages)
                for m in reversed([m for m in msgs if m.get("messageId") not in self.seen]):
                    await self._handle(m)
            except Exception as exc:
                LOG.warning("兜底轮询失败：%s", exc)

    def _kill_stale_streams(self) -> None:
        """清理上次运行残留的事件流进程（应用重启后子进程会变成孤儿，导致重复订阅）。"""
        import glob
        import signal
        me = os.getpid()
        for d in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(d.split("/")[-1])
            except ValueError:
                continue
            if pid == me:
                continue
            try:
                parts = open(f"{d}/cmdline", "rb").read().decode("utf-8", "replace").split("\x00")
            except OSError:
                continue
            joined = " ".join(parts)
            if "listen-im" in joined and getattr(self.cfg, "dws_config_dir", "") in joined:
                try:
                    os.kill(pid, signal.SIGTERM)
                    LOG.info("清理残留事件流进程 %s", pid)
                except OSError:
                    pass

    async def _listen_once(self) -> None:
        await asyncio.to_thread(self._kill_stale_streams)
        args = [self.cfg.dws_bin, "event", "+listen-im", "--kind", "group",
                "--chat-id", self.cfg.group_chat_id, "-f", "ndjson"]
        env = dict(os.environ)
        env["DWS_CONFIG_DIR"] = getattr(self.cfg, "dws_config_dir", "")
        env["DWS_KEYCHAIN_DIR"] = getattr(self.cfg, "dws_keychain_dir", "")
        env["DWS_NO_UPDATE_CHECK"] = "1"
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=self.cfg.inbox_dir)
        LOG.info("事件流已连接（conversationId=%s）", self.cfg.group_chat_id)
        stderr_buf = []

        async def _drain_err():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                stderr_buf.append(line.decode("utf-8", "replace").rstrip())
                if len(stderr_buf) > 40:
                    del stderr_buf[:20]

        err_task = asyncio.create_task(_drain_err())
        try:
            while not self.stop:
                line = await proc.stdout.readline()
                if not line:
                    LOG.warning("事件流 EOF（进程可能已退出），stderr: %s", " | ".join(stderr_buf[-5:]))
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue
                try:
                    evt = json.loads(text)
                except Exception:
                    continue
                await self._on_event(evt)
        finally:
            if proc.returncode is None:
                proc.terminate()
            err_task.cancel()
            LOG.info("事件流断开（returncode=%s），准备重连；stderr 尾部：%s",
                     proc.returncode, " | ".join(stderr_buf[-4:]))

    async def _on_event(self, evt: dict) -> None:
        etype = evt.get("type") or ""
        if etype != "user_im_message_receive_group":
            return
        conv = evt.get("conversation_id") or ""
        if self.cfg.group_chat_id and conv != self.cfg.group_chat_id:
            return
        ids = []
        if evt.get("message_id"):
            ids.append(evt["message_id"])
        for inner in evt.get("forward_messages") or []:
            if inner.get("message_id"):
                ids.append(inner["message_id"])
        ids = [i for i in dict.fromkeys(ids) if i not in self.seen]
        if not ids:
            return
        LOG.info("事件触发：%s @%s，待取消息 %d 条", evt.get("create_time"), evt.get("sender"), len(ids))
        try:
            msgs = await asyncio.to_thread(self.mget, ids)
        except Exception as exc:
            LOG.warning("取消息详情失败：%s", exc)
            return
        for m in msgs:
            if m.get("messageId") not in self.seen:
                await self._handle(m)
