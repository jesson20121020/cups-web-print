"""后台服务生命周期：机器人（Stream）+ 群监听（事件中继/轮询）。

在 Flask 进程内以线程方式运行，页面保存配置后可热重启，不用重启应用。
"""
import asyncio
import logging
import os
import threading
import time

LOG = logging.getLogger("dingtalk.service")


class Service:
    def __init__(self, store, dws):
        self.store = store
        self.dws = dws
        self.thread = None
        self.loop = None
        self.handler = None
        self.watcher = None
        self.client = None
        self.state = "stopped"        # stopped | starting | running | error
        self.last_error = ""
        self.started_at = 0
        self.events = []              # 最近事件（给页面看）
        self._lock = threading.Lock()

    # ---------- 状态 ----------
    def status(self) -> dict:
        cfg = self.store.settings()
        auth = self.dws.auth_status()
        return {
            "state": self.state,
            "last_error": self.last_error,
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "dws_installed": self.dws.installed(),
            "dws_version": self.dws.version(),
            "authenticated": auth.get("authenticated", False),
            "user_name": auth.get("user_name", ""),
            "corp_name": auth.get("corp_name", ""),
            "group_enabled": cfg.group_enabled,
            "group_name": cfg.group_name,
            "group_mode": cfg.group_mode,
            "events": self.events[-20:],
        }

    def log_event(self, kind: str, text: str) -> None:
        self.events.append({"t": time.strftime("%H:%M:%S"), "kind": kind, "text": text})
        if len(self.events) > 200:
            del self.events[:100]

    # ---------- 生命周期 ----------
    def start(self) -> dict:
        with self._lock:
            if self.state == "running":
                return {"ok": True, "state": self.state}
            cfg = self.store.settings()
            try:
                cfg.validate()
            except SystemExit as exc:
                self.state = "error"
                self.last_error = str(exc)
                return {"ok": False, "error": self.last_error}
            self.state = "starting"
            self.last_error = ""
            self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self.thread.start()
            return {"ok": True, "state": self.state}

    def stop(self) -> dict:
        with self._lock:
            if self.watcher:
                self.watcher.stop = True
            from . import inbox_server
            inbox_server.set_target(None)
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            self.state = "stopped"
            self.started_at = 0
            return {"ok": True, "state": self.state}

    def restart(self) -> dict:
        self.stop()
        time.sleep(0.5)
        return self.start()

    # ---------- 主线程 ----------
    def _run(self, cfg) -> None:
        try:
            from dingtalk_stream import DingTalkStreamClient, Credential, chatbot
            from dingtalk_stream import Card_Callback_Router_Topic
            from .bot import PrintBotHandler
            from .card_callback import CardActionHandler
            from .dingtalk_api import DingTalkAPI
            from .group_watcher import GroupEventWatcher, GroupWatcher
            from .print_client import PrintClient

            api = DingTalkAPI(cfg.client_id, cfg.client_secret, cfg.robot_code)
            pc = PrintClient(cfg.print_service, cfg.printer_name, cfg.request_timeout)
            handler = PrintBotHandler(cfg, api, pc)
            handler.ensure_card_template()
            self.handler = handler

            client = DingTalkStreamClient(Credential(cfg.client_id, cfg.client_secret))
            client.register_callback_handler(chatbot.ChatbotMessage.TOPIC, handler)
            client.register_callback_handler(Card_Callback_Router_Topic, CardActionHandler(handler))
            self.client = client

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop

            async def _amain():
                if cfg.group_enabled:
                    if cfg.group_mode == "event":
                        watcher = GroupEventWatcher(cfg, handler)   # 应用内直接跑事件流，无需中继
                    else:
                        watcher = GroupWatcher(cfg, handler)        # 轮询 + HTTP 中继（兼容容器部署）
                        from . import inbox_server
                        inbox_server.set_target(watcher)
                        inbox_server.start_once(cfg.event_port)
                    watcher.bind_loop(loop)
                    self.watcher = watcher
                    loop.create_task(watcher.run())
                    LOG.info("群监听已启动：%s（%s）", cfg.group_name, cfg.group_mode)

                self.state = "running"
                self.started_at = time.time()
                self.log_event("info", "机器人已启动")
                LOG.info("钉钉机器人已启动")

                while True:                    # 断线重连
                    try:
                        await client.start()   # 在既有 loop 里跑，别用 start_forever
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOG.warning("Stream 断开，5 秒重连：%s", exc)
                        await asyncio.sleep(5)

            try:
                loop.run_until_complete(_amain())
            except RuntimeError as exc:
                # 主动 stop() 会关闭 loop，这里不是真失败
                if "Event loop stopped" in str(exc) or "Event loop is closed" in str(exc):
                    LOG.info("服务已停止")
                else:
                    raise
        except Exception as exc:
            LOG.exception("钉钉服务启动失败")
            self.state = "error"
            self.last_error = str(exc)
            self.log_event("error", f"启动失败：{exc}")
