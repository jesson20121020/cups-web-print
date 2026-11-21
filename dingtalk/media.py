"""钉钉图片素材：上传到钉钉 CDN，并按 media_id 发图片消息。

为什么不用本地/局域网地址：
  卡片和图片消息里的图片都是钉钉客户端去拉取的，家用 NAS 没有公网地址，
  手机走 4G 时局域网 IP 根本不可达。所以图片先 `media/upload` 上传到钉钉，
  拿到 `@lALP...` 形式的 media_id，客户端会用
  https://static.dingtalk.com/media/<id>_<宽>_<高>.png 这种公开地址渲染，
  哪里都能看。

两类 access_token：
  - v1.0（api.dingtalk.com）：发消息、卡片
  - oapi（oapi.dingtalk.com）：上传素材
"""
import json
import logging
import threading
import time

import requests

LOG = logging.getLogger("bridge.media")

API = "https://api.dingtalk.com"
OAPI = "https://oapi.dingtalk.com"


class MediaError(RuntimeError):
    pass


class MediaClient:
    def __init__(self, client_id: str, client_secret: str, robot_code: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.robot_code = robot_code or client_id
        self._api_token = ""
        self._api_exp = 0.0
        self._oapi_token = ""
        self._oapi_exp = 0.0
        self._lock = threading.Lock()

    # ---------- token ----------
    def api_token(self) -> str:
        with self._lock:
            if self._api_token and time.time() < self._api_exp - 120:
                return self._api_token
            r = requests.post(f"{API}/v1.0/oauth2/accessToken",
                              json={"appKey": self.client_id, "appSecret": self.client_secret},
                              timeout=15)
            r.raise_for_status()
            d = r.json()
            self._api_token = d["accessToken"]
            self._api_exp = time.time() + int(d.get("expireIn", 7200))
            return self._api_token

    def oapi_token(self) -> str:
        with self._lock:
            if self._oapi_token and time.time() < self._oapi_exp - 120:
                return self._oapi_token
            r = requests.get(f"{OAPI}/gettoken",
                             params={"appkey": self.client_id, "appsecret": self.client_secret},
                             timeout=15)
            r.raise_for_status()
            d = r.json()
            if d.get("errcode"):
                raise MediaError(f"取 oapi token 失败: {d}")
            self._oapi_token = d["access_token"]
            self._oapi_exp = time.time() + int(d.get("expires_in", 7200))
            return self._oapi_token

    # ---------- 上传 ----------
    def upload_image(self, path: str) -> str:
        """上传一张图片，返回 media_id（形如 @lALPM1ntx...）。"""
        tok = self.oapi_token()
        with open(path, "rb") as fh:
            files = {"media": (path.rsplit("/", 1)[-1], fh, "image/png")}
            r = requests.post(f"{OAPI}/media/upload",
                              params={"access_token": tok, "type": "image"},
                              files=files, timeout=60)
        r.raise_for_status()
        d = r.json()
        if d.get("errcode"):
            raise MediaError(f"上传素材失败: {d}")
        mid = d.get("media_id") or ""
        if not mid:
            raise MediaError(f"上传素材没返回 media_id: {r.text[:200]}")
        LOG.info("素材已上传 %s (%d bytes)", mid, len(open(path, "rb").read()))
        return mid

    # ---------- 发图片消息 ----------
    def send_image(self, media_id: str, *, open_conversation_id: str = "",
                   staff_ids: list[str] | None = None) -> bool:
        """群里发图片（openConversationId）或单聊发图片（staffIds）。"""
        token = self.api_token()
        headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
        msg_param = json.dumps({"photoURL": media_id}, ensure_ascii=False)
        try:
            if open_conversation_id:
                body = {"robotCode": self.robot_code, "openConversationId": open_conversation_id,
                        "msgKey": "sampleImageMsg", "msgParam": msg_param}
                path = "/v1.0/robot/groupMessages/send"
            else:
                body = {"robotCode": self.robot_code, "userIds": list(staff_ids or []),
                        "msgKey": "sampleImageMsg", "msgParam": msg_param}
                path = "/v1.0/robot/oToMessages/batchSend"
            r = requests.post(f"{API}{path}", headers=headers, json=body, timeout=20)
            if r.status_code != 200:
                LOG.warning("发图片消息失败 HTTP %s: %s", r.status_code, r.text[:300])
                return False
            LOG.info("图片消息已发送 media=%s → %s", media_id,
                     open_conversation_id or ",".join(staff_ids or []))
            return True
        except Exception as exc:  # 发图失败不能影响打印主流程
            LOG.warning("发图片消息异常: %s", exc)
            return False
