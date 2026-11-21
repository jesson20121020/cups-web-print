#!/usr/bin/env python3
"""钉钉应用/机器人自检：判断「凭证对不对」和「机器人到底存不存在」。

    python robot_probe.py            # 用 .env 里的 AppKey/Secret
    python robot_probe.py --user xxx # 顺带试着给这个 userId 发一条（可选）

判读：
  invalidParameter.robotCode.notExsit  -> 开放平台里这个应用的「机器人」没启用，或版本没发布
  staffId not exist / user not found   -> 机器人正常，只是 userId 不对（说明机器人已生效）
  成功(200)                             -> 机器人和都在线，去钉钉里搜机器人名字即可
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from bridge.config import Config  # noqa: E402
from bridge.dingtalk_api import DingTalkAPI, API  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="nonexistent_user_probe_zzz")
    ap.add_argument("--robot-code", default=None)
    args = ap.parse_args()

    cfg = Config()
    cfg.validate()
    robot = args.robot_code or cfg.robot_code

    print(f"AppKey   : {cfg.client_id}")
    print(f"robotCode: {robot}" + ("" if robot == cfg.client_id else "  (与 AppKey 不同，注意是否填对)"))

    api = DingTalkAPI(cfg.client_id, cfg.client_secret, robot)
    try:
        token = api.token()
        print(f"凭证     : ✅ 可用（token {token[:8]}…）")
    except Exception as exc:
        print(f"凭证     : ❌ {exc}")
        return 3

    r = requests.post(
        f"{API}/v1.0/robot/oToMessages/batchSend",
        headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
        json={"robotCode": robot, "userIds": [args.user], "msgKey": "sampleText",
              "msgParam": json.dumps({"content": "probe"}, ensure_ascii=False)},
        timeout=15,
    )
    body = r.text[:400]
    print(f"机器人探测: HTTP {r.status_code} {body}")

    if "robotCode.notExsit" in body:
        print("\n➡️  结论：这个应用的**机器人不存在**。")
        print("   1) 开放平台 → 你的应用 → 「机器人」→ 添加/启用机器人，消息接收模式选 Stream 模式，保存")
        print("   2) 「版本管理与发布」→ 创建版本 → 可见范围包含你自己 → 发布")
        print("   3) 发布后再跑一次本脚本；然后去钉钉搜索机器人名字进单聊")
        return 4
    if r.status_code == 200 or "user" in body.lower():
        print("\n➡️  结论：机器人已生效（只是探测用的 userId 不存在）。去钉钉搜索机器人名字即可。")
        return 0
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
