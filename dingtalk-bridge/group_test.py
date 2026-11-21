#!/usr/bin/env python3
"""一次性测试：把「木耳作业」群里最近一条带附件的消息抓下来，发一张打印卡片。

    python group_test.py            # 最近 15 条里最新的一条带资源消息
    python group_test.py --dry-run  # 只下载，不发卡片
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge.bot import Pending          # noqa: E402
from bridge.config import Config        # noqa: E402
from bridge.dingtalk_api import DingTalkAPI  # noqa: E402
from bridge.group_watcher import GroupWatcher  # noqa: E402
from bridge.print_client import PrintClient    # noqa: E402


async def main() -> int:
    cfg = Config()
    cfg.validate()
    api = DingTalkAPI(cfg.client_id, cfg.client_secret, cfg.robot_code)
    pc = PrintClient(cfg.print_service, cfg.printer_name, cfg.request_timeout)

    from bridge.bot import PrintBotHandler
    handler = PrintBotHandler(cfg, api, pc)
    handler.card_template_id = cfg.card_template_id
    w = GroupWatcher(cfg, handler)

    msgs = w.fetch_messages()
    print(f"拉到 {len(msgs)} 条消息")
    target = next((m for m in msgs if m.get("resourceRefs")), None)
    if not target:
        print("最近消息里没有带附件的内容")
        return 1
    print(f"目标消息：{target.get('createTime')} @{target.get('sender')} 资源 {len(target['resourceRefs'])} 个")

    atts = []
    for idx, ref in enumerate(target["resourceRefs"]):
        path = w.download(ref, w._pick_name(target, ref, idx))
        print("  下载 ->", path, os.path.getsize(path), "bytes")
        from bridge.bot import Attachment
        atts.append(Attachment(path, os.path.basename(path)))

    if "--dry-run" in sys.argv:
        print("dry-run：不发送卡片")
        return 0

    pending = Pending(conversation_id=target.get("conversationId", ""),
                      conversation_type="1" if cfg.card_target_staff_id else "2",
                      staff_id=cfg.card_target_staff_id or target.get("senderId", ""))
    pending.attachments = atts
    ok = await handler._send_card(pending)
    print("卡片发送：", "成功" if ok else "失败")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
