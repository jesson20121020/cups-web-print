#!/usr/bin/env python3
"""给某个钉钉用户发一张测试卡片（验证模板渲染 + 按钮回调链路）。

    python card_test.py 0344694131836
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge.card_api import CardAPI      # noqa: E402
from bridge.config import Config         # noqa: E402
from bridge.dingtalk_api import DingTalkAPI  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python card_test.py <staffId>")
        return 2
    staff_id = sys.argv[1]
    cfg = Config()
    cfg.validate()
    if not cfg.card_template_id:
        print("❌ .env 里没有 CARD_TEMPLATE_ID")
        return 3

    api = DingTalkAPI(cfg.client_id, cfg.client_secret, cfg.robot_code)
    cards = CardAPI(api)
    content = ("作业1.jpg\n作业2.jpg\n\nA4 · 彩色 · 双面 · 1 份\n"
               "多张图片会合并成一个 PDF")
    tid = cards.send(cfg.card_template_id, {
        "title": "待确认打印", "meta": "2 个文件 · 确认后开始打印",
        "content": content, "progress": 0,
        "btnPrint": "🖨 打印", "btnCancel": "取消"},
                     conversation_type="1", conversation_id=staff_id, staff_id=staff_id,
                     robot_code=cfg.robot_code,
                     out_track_id=f"test-{int(time.time())}")
    print("✅ 已投放测试卡片 outTrackId =", tid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
