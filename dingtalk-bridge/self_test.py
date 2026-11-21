#!/usr/bin/env python3
"""不依赖钉钉的自检脚本：验证「打印服务链路」这一半是通的。

    python self_test.py --status                 # 打印服务 + 打印机状态
    python self_test.py --upload 某个.jpg        # 只上传，不出纸（安全）
    python self_test.py --print 某个.pdf --mono --copies 1   # 真打（会出纸！）

钉钉那一半（Stream 连接、消息收发）只能拿真 AppKey 跑：
    python -m bridge.main --check
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge.config import Config          # noqa: E402
from bridge.print_client import PrintClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--upload")
    ap.add_argument("--print", dest="print_file")
    ap.add_argument("--mono", action="store_true")
    ap.add_argument("--simplex", action="store_true")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--paper", default=None)
    ap.add_argument("--operator", default="")   # 打印记录里标注是谁打的
    args = ap.parse_args()

    cfg = Config()
    pc = PrintClient(cfg.print_service, cfg.printer_name, cfg.request_timeout)

    if args.status or not (args.upload or args.print_file):
        try:
            printers = pc.printers()
            print(f"服务 {cfg.print_service} 可用：{[p['name'] for p in printers]}")
            h = pc.health()
            print(f"打印机 {h['printer']} online={h['online']} {h.get('reason', '')}")
            return 0
        except Exception as exc:
            print(f"❌ {exc}")
            return 2

    path = args.upload or args.print_file
    if not os.path.isfile(path):
        print(f"❌ 文件不存在：{path}")
        return 1

    if args.upload:
        name = pc.upload(path)
        print(f"✅ 上传成功：{name}（未打印）")
        return 0

    payload = {
        "color": "mono" if args.mono else cfg.default_color,
        "duplex": "one-sided" if args.simplex else cfg.default_duplex,
        "paper": args.paper or cfg.default_paper,
        "copies": args.copies,
    }
    print("提交参数：", json.dumps(payload, ensure_ascii=False))
    h = pc.health()
    if not h["online"]:
        print(f"❌ 打印机不在线，先不打：{h['reason']}")
        return 2
    name = pc.upload(path)
    resp = pc.submit(name, printer=h["printer"], **payload)
    job_id = resp["job_id"]
    print(f"已提交 job={job_id}，等待结果…")
    info = pc.wait(job_id, cfg.job_timeout)
    print(f"终态：{info.get('status')} {info.get('message') or ''}")
    return 0 if (info.get("status") or "").lower() == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
