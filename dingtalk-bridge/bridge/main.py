"""入口：建立钉钉 Stream 长连接（只出站，不需要公网 IP / 端口映射）。"""
import asyncio
import logging
import sys
import threading
import time

import dingtalk_stream

from .bot import PrintBotHandler
from .card_callback import CardActionHandler
from .group_watcher import GroupEventWatcher, GroupWatcher
from . import inbox_server
from .config import Config
from .dingtalk_api import DingTalkAPI
from .print_client import PrintClient

LOG = logging.getLogger("bridge.main")


def setup_logger() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)-14s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build(cfg: Config):
    api = DingTalkAPI(cfg.client_id, cfg.client_secret, cfg.robot_code)
    pc = PrintClient(cfg.print_service, cfg.printer_name, cfg.request_timeout)
    return api, pc


def check(cfg: Config) -> int:
    api, pc = build(cfg)
    code = 0
    try:
        printers = pc.printers()
        print(f"✅ 打印服务 {cfg.print_service} 可用，队列: {[p['name'] for p in printers]}")
        health = pc.health()
        print(f"   打印机 {health['printer']}: online={health['online']} {health.get('reason', '')}")
    except Exception as exc:
        print(f"❌ 打印服务不可用: {exc}")
        code = 2
    try:
        tok = api.token()
        print(f"✅ 钉钉 access_token 获取成功（{tok[:6]}…）")
    except Exception as exc:
        print(f"❌ 钉钉凭证有问题: {exc}")
        code = 3
    return code


def main() -> None:
    setup_logger()
    cfg = Config()
    cfg.validate()

    if "--check" in sys.argv:
        raise SystemExit(check(cfg))

    api, pc = build(cfg)
    handler = PrintBotHandler(cfg, api, pc)

    credential = dingtalk_stream.Credential(cfg.client_id, cfg.client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, handler)
    client.register_callback_handler(dingtalk_stream.Card_Callback_Router_Topic, CardActionHandler(handler))
    handler.ensure_card_template()

    LOG.info("Stream 机器人启动：appKey=%s… robotCode=%s 打印服务=%s",
             cfg.client_id[:6], cfg.robot_code[:6], cfg.print_service)
    LOG.info("多图合并窗口 %ds，待确认有效期 %dmin，白名单 %s",
             cfg.batch_window, cfg.pending_ttl, cfg.allow_user_ids or "未限制")

    if cfg.group_enabled:
        cls = GroupEventWatcher if cfg.group_mode == "event" else GroupWatcher
        watcher = cls(cfg, handler)
        LOG.info("群监听模式：%s", cfg.group_mode)

        def _run_watcher():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            watcher.bind_loop(loop)
            if cfg.group_mode == "event":
                loop.run_until_complete(watcher.run())
            else:
                loop.create_task(watcher.run())
                inbox_server.start(watcher, cfg.event_port)
                loop.run_forever()

        threading.Thread(target=_run_watcher, daemon=True).start()
        LOG.info("群监听线程已启动（群：%s）", cfg.group_name)

    while True:  # SDK 断线会抛异常，这里兜底重连
        try:
            client.start_forever()
        except KeyboardInterrupt:
            LOG.info("收到退出信号")
            break
        except Exception:
            LOG.exception("Stream 连接断开，5 秒后重连")
            time.sleep(5)


if __name__ == "__main__":
    main()
