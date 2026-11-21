"""钉钉开放平台最小客户端：拿 access_token、把机器人收到的文件下载到本地。"""
import logging
import os
import threading
import time

import requests

LOG = logging.getLogger("bridge.dingtalk")

API = "https://api.dingtalk.com"


class DingTalkAPI:
    def __init__(self, client_id: str, client_secret: str, robot_code: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.robot_code = robot_code or client_id
        self._token = None
        self._token_expire_at = 0
        self._lock = threading.Lock()

    # ---------- token ----------
    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expire_at - 120:
                return self._token
            r = requests.post(
                f"{API}/v1.0/oauth2/accessToken",
                json={"appKey": self.client_id, "appSecret": self.client_secret},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["accessToken"]
            self._token_expire_at = time.time() + int(data.get("expireIn", 7200))
            return self._token

    def _headers(self) -> dict:
        return {"x-acs-dingtalk-access-token": self.token(), "Content-Type": "application/json"}

    # ---------- 下载机器人收到的文件 ----------
    def get_download_url(self, download_code: str) -> str:
        r = requests.post(
            f"{API}/v1.0/robot/messageFiles/download",
            headers=self._headers(),
            json={"downloadCode": download_code, "robotCode": self.robot_code},
            timeout=20,
        )
        r.raise_for_status()
        url = (r.json() or {}).get("downloadUrl")
        if not url:
            raise RuntimeError(f"钉钉未返回 downloadUrl: {r.text[:300]}")
        return url

    def download(self, download_code: str, dest_path: str, max_bytes: int = 100 * 1024 * 1024) -> str:
        url = self.get_download_url(download_code)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            written = 0
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        fh.close()
                        os.unlink(dest_path)
                        raise RuntimeError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限")
                    fh.write(chunk)
        LOG.info("下载完成 %s (%d bytes)", os.path.basename(dest_path), written)
        return dest_path
