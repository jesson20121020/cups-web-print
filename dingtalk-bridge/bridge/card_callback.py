"""互动卡片按钮点击回调（Stream 模式，topic: /v1.0/card/instances/callback）。"""
import asyncio
import logging

import dingtalk_stream
from dingtalk_stream import AckMessage, CardCallbackMessage

LOG = logging.getLogger("bridge.card_cb")


class CardActionHandler(dingtalk_stream.CallbackHandler):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        data = callback.data or {}
        try:
            msg = CardCallbackMessage.from_dict(data)
        except Exception:
            LOG.exception("解析卡片回调失败: %s", str(data)[:400])
            return AckMessage.STATUS_OK, "OK"

        action = self._action_id(msg.content, data)
        LOG.info("收到卡片回调 action=%s outTrackId=%s user=%s content=%s",
                 action, msg.card_instance_id, msg.user_id, str(msg.content)[:400])

        task = asyncio.create_task(self.bot.on_card_action(msg.card_instance_id, action, msg.user_id))
        self.bot._bg.add(task)
        task.add_done_callback(self.bot._bg.discard)
        return AckMessage.STATUS_OK, "OK"

    @staticmethod
    def _action_id(content, data) -> str:
        """不同模板版本回调结构略有差异，这里都兜一遍。"""
        try:
            private = (content or {}).get("cardPrivateData") or {}
            ids = private.get("actionIds") or []
            if ids:
                return str(ids[0])
            for key in ("actionId", "callbackId", "action"):
                if (content or {}).get(key):
                    return str((content or {})[key])
            params = private.get("params") or (content or {}).get("params") or {}
            for key in ("actionId", "callbackId", "action"):
                if params.get(key):
                    return str(params[key])
            # 有时候直接是 {"confirm": ...}
            for key in ("confirm", "cancel"):
                if key in (params or {}):
                    return key
        except Exception:
            LOG.exception("解析 actionId 失败: %s", str(content)[:300])
        return ""
