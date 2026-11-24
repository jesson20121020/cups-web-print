"""cups-web-print 的 HTTP 客户端：上传 -> 打印 -> 等结果。

走的就是网页「打印」按钮的同一条链路（/api/upload → /api/print → /api/jobs），
所以网页能打的东西这里都能打。
"""
import logging
import os
import time

import requests

LOG = logging.getLogger("bridge.print")


class PrintError(RuntimeError):
    pass


class PrintClient:
    def __init__(self, base_url: str, printer_name: str = "", timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.printer_name = printer_name
        self.timeout = timeout
        self._session = requests.Session()

    # ---------- 状态 ----------
    def printers(self) -> list:
        r = self._session.get(f"{self.base}/api/printers", timeout=self.timeout)
        r.raise_for_status()
        return (r.json() or {}).get("printers", [])

    def probe(self, printer: str) -> dict:
        r = self._session.get(f"{self.base}/api/printer/{printer}/status", timeout=self.timeout)
        if r.status_code != 200:
            return {"name": printer, "online_status": "unknown", "status": "unknown"}
        return r.json() or {}

    def resolve_printer(self) -> str:
        if self.printer_name:
            return self.printer_name
        printers = self.printers()
        if not printers:
            raise PrintError("打印服务里没有任何打印机队列")
        # 优先 IPP 队列（TS3380-IPPS 这类），避开 TEST 队列
        for p in printers:
            name = p.get("name", "")
            if "IPP" in name.upper():
                return name
        return printers[0]["name"]

    def health(self) -> dict:
        """返回 {online: bool, reason: str, printer: str}"""
        printer = self.resolve_printer()
        info = self.probe(printer)
        online = info.get("online_status")
        if online == "offline":
            return {"online": False, "reason": info.get("offline_reason") or "打印机离线", "printer": printer}
        return {"online": True, "reason": "", "printer": printer, "raw": info}

    # ---------- 上传 ----------
    def upload(self, file_path: str) -> str:
        name = os.path.basename(file_path)
        with open(file_path, "rb") as fh:
            files = {"file": (name, fh, "application/octet-stream")}
            r = self._session.post(f"{self.base}/api/upload", files=files, timeout=300)
        if r.status_code != 200:
            raise PrintError(f"上传失败 HTTP {r.status_code}: {_short(r.text)}")
        data = r.json() or {}
        server_name = data.get("filename") or data.get("name") or data.get("file")
        if not server_name:
            raise PrintError(f"上传返回里没有文件名: {_short(r.text)}")
        LOG.info("上传成功 %s -> %s", name, server_name)
        return server_name

    # ---------- 打印 ----------
    def submit(self, server_name: str, *, printer: str = "", color: str = "color", duplex: str = "two-sided-long-edge",
               paper: str = "A4", paper_type: str = "plain", orientation: str = "portrait",
               copies: int = 1, page_range=None, scaling: str = "fit") -> dict:
        payload = {
            "filename": server_name,
            "printer": printer or self.resolve_printer(),
            "color_mode": color,
            "duplex": duplex,
            "orientation": orientation,
            "paper_size": paper,
            "paper_type": paper_type,
            "copies": int(copies),
            "page_range": page_range or None,
            "mirror": False,
            "print_scaling": scaling,
        }
        r = self._session.post(f"{self.base}/api/print", json=payload, timeout=300)
        data = {}
        try:
            data = r.json() or {}
        except Exception:
            pass
        if r.status_code != 200 or not data.get("success"):
            raise PrintError(data.get("error") or f"提交打印失败 HTTP {r.status_code}: {_short(r.text)}")
        LOG.info("打印已提交 job=%s %s", data.get("job_id"), server_name)
        return data

    def job(self, job_id: str) -> dict:
        r = self._session.get(f"{self.base}/api/jobs", timeout=self.timeout)
        r.raise_for_status()
        for j in (r.json() or {}).get("jobs", []):
            if j.get("id") == job_id or j.get("job_id") == job_id:
                return j
        return {}

    def wait(self, job_id: str, timeout: int = 300, interval: float = 2.0) -> dict:
        """轮询到终态。返回最后一个 job 快照（含 status/message）。"""
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            try:
                last = self.job(job_id) or last
            except Exception as exc:  # 服务抖一下不要立刻判死
                LOG.warning("查询 job 失败: %s", exc)
            status = (last.get("status") or "").lower()
            if status in ("completed", "failed", "cancelled", "canceled"):
                return last
            time.sleep(interval)
        last = dict(last or {})
        last["status"] = "timeout"
        return last


def _short(text: str, n: int = 200) -> str:
    text = (text or "").replace("\n", " ")
    return text[:n]
