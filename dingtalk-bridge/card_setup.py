#!/usr/bin/env python3
"""互动卡片模板自检/创建工具。

    python card_setup.py --list     # 看已有模板（验证权限是否开通）
    python card_setup.py --create   # 按 card_spec 建一个模板，把 ID 打印出来

需要应用权限：Card.Template.ReadWrite.All、Card.Instance.Write
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import card_spec  # noqa: E402
from bridge.card_api import CardAPI  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.dingtalk_api import DingTalkAPI  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--template-id", default="")
    args = ap.parse_args()

    cfg = Config()
    cfg.validate()
    api = DingTalkAPI(cfg.client_id, cfg.client_secret, cfg.robot_code)
    cards = CardAPI(api)

    if args.template_id:
        print(f"用已有模板：{args.template_id}")
        print("把它写进 .env：CARD_TEMPLATE_ID=" + args.template_id)
        return 0

    if args.list or not args.create:
        try:
            items = cards.list_templates()
            print(f"✅ 模板接口可用，共 {len(items)} 个模板")
            for it in items[:20]:
                print("  -", it if isinstance(it, str) else it.get("cardTemplateId") or it)
        except Exception as exc:
            print("❌", exc)
            return 2

    if args.create:
        try:
            tid = cards.create_template(card_spec.TEMPLATE_NAME, card_spec.schema())
            print(f"✅ 模板已创建：{tid}")
            print("把它写进 .env：CARD_TEMPLATE_ID=" + tid)
            return 0
        except Exception as exc:
            print("❌", exc)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
