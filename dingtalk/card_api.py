"""互动卡片开放接口：建模板 / 投放卡片 / 更新卡片。

接口（都是 v1.0，需要应用权限 Card.Template.ReadWrite.All + Card.Instance.Write）：
  POST /v1.0/card/templates            建模板
  POST /v1.0/card/instances            建卡片实例
  POST /v1.0/card/instances/deliver    把实例投到会话
  PUT  /v1.0/card/instances            原地更新卡片内容
"""
import json
import logging
import uuid

import requests

from .dingtalk_api import API, DingTalkAPI

LOG = logging.getLogger("bridge.card")


class CardError(RuntimeError):
    pass


class CardAPI:
    def __init__(self, api: DingTalkAPI):
        self.api = api

    # ---------- 模板 ----------
    def create_template(self, name: str, schema: dict) -> str:
        """返回 cardTemplateId（形如 xxx.schema）。"""
        body = {"name": name, "schema": json.dumps(schema, ensure_ascii=False)}
        r = requests.post(f"{API}/v1.0/card/templates", headers=self.api._headers(), json=body, timeout=20)
        if r.status_code != 200:
            raise CardError(f"建模板失败 HTTP {r.status_code}: {r.text[:400]}")
        data = r.json() or {}
        tid = data.get("cardTemplateId") or data.get("templateId") or data.get("id")
        if not tid:
            raise CardError(f"建模板返回里没有模板 ID: {r.text[:300]}")
        LOG.info("卡片模板已创建：%s", tid)
        return tid

    def list_templates(self) -> list:
        r = requests.get(f"{API}/v1.0/card/templates", headers=self.api._headers(), timeout=20)
        if r.status_code != 200:
            raise CardError(f"查模板失败 HTTP {r.status_code}: {r.text[:300]}")
        data = r.json() or {}
        return data.get("templates") or data.get("data") or []

    # ---------- 卡片实例 ----------
    def send(self, template_id: str, params: dict, *, conversation_type: str, conversation_id: str,
             staff_id: str, robot_code: str, out_track_id: str = "") -> str:
        """建实例 + 投放，返回 outTrackId。"""
        out_track_id = out_track_id or f"print-{uuid.uuid4().hex[:20]}"
        create_body = {
            "cardTemplateId": template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": {k: str(v) for k, v in params.items()}},
            "callbackType": "STREAM",
            "imGroupOpenSpaceModel": {"supportForward": True},
            "imRobotOpenSpaceModel": {"supportForward": True},
        }
        r = requests.post(f"{API}/v1.0/card/instances", headers=self.api._headers(), json=create_body, timeout=20)
        if r.status_code != 200:
            raise CardError(f"建卡片实例失败 HTTP {r.status_code}: {r.text[:400]}")

        deliver = {"outTrackId": out_track_id, "userIdType": 1}
        if conversation_type == "2":
            deliver["openSpaceId"] = f"dtv1.card//IM_GROUP.{conversation_id}"
            deliver["imGroupOpenDeliverModel"] = {"robotCode": robot_code}
        else:
            deliver["openSpaceId"] = f"dtv1.card//IM_ROBOT.{staff_id}"
            deliver["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}

        r = requests.post(f"{API}/v1.0/card/instances/deliver", headers=self.api._headers(), json=deliver, timeout=20)
        if r.status_code != 200:
            raise CardError(f"投放卡片失败 HTTP {r.status_code}: {r.text[:400]}")
        LOG.info("卡片已投放 outTrackId=%s", out_track_id)
        return out_track_id

    def update(self, out_track_id: str, params: dict) -> bool:
        body = {
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": {k: str(v) for k, v in params.items()}},
        }
        r = requests.put(f"{API}/v1.0/card/instances", headers=self.api._headers(), json=body, timeout=20)
        if r.status_code != 200:
            LOG.warning("更新卡片失败 HTTP %s: %s", r.status_code, r.text[:300])
            return False
        return True
