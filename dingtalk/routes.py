"""钉钉打印集成：页面与接口。"""
import json
import logging

from flask import Blueprint, jsonify, render_template, request

from .dws import Dws
from .service import Service
from .store import Store, store as default_store

LOG = logging.getLogger("dingtalk.routes")

bp = Blueprint("dingtalk", __name__)

_store = default_store
_dws = Dws(_store.data_dir)
_service = Service(_store, _dws)


def init(app) -> None:
    """注册蓝图并在启动时按配置拉起后台。"""
    app.register_blueprint(bp)
    cfg = _store.load()
    if cfg.get("client_id") and cfg.get("client_secret"):
        try:
            _service.start()
        except Exception as exc:
            LOG.warning("钉钉服务未能自动启动：%s", exc)


# ---------------- 页面 ----------------
@bp.get("/dingtalk")
def page():
    return render_template("dingtalk.html", version=_version())


def _version() -> str:
    try:
        with open("version", encoding="utf-8") as fh:
            return "v" + fh.read().strip()
    except OSError:
        return ""


# ---------------- 配置 ----------------
@bp.get("/api/dingtalk/config")
def get_config():
    return jsonify({"ok": True, "config": _store.public()})


@bp.post("/api/dingtalk/config")
def save_config():
    data = request.get_json(silent=True) or {}
    saved = _store.save(data)
    restart = bool(data.get("_restart"))
    if restart and saved.get("client_id") and saved.get("client_secret"):
        _service.restart()
    return jsonify({"ok": True, "config": _store.public()})


# ---------------- 状态 ----------------
@bp.get("/api/dingtalk/status")
def status():
    return jsonify({"ok": True, **_service.status()})


@bp.post("/api/dingtalk/service/<action>")
def service_action(action: str):
    if action == "start":
        return jsonify(_service.start())
    if action == "stop":
        return jsonify(_service.stop())
    if action == "restart":
        return jsonify(_service.restart())
    return jsonify({"ok": False, "error": "unknown action"}), 400


# ---------------- CLI ----------------
@bp.post("/api/dingtalk/dws/install")
def dws_install():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **_dws.install(mirror=body.get("mirror", "github"))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/api/dingtalk/dws/version")
def dws_version():
    return jsonify({"ok": True, "installed": _dws.installed(), "version": _dws.version()})


# ---------------- 授权 ----------------
@bp.post("/api/dingtalk/login/start")
def login_start():
    try:
        info = _dws.login_start()
        return jsonify({"ok": True, **info})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.get("/api/dingtalk/login/poll")
def login_poll():
    try:
        info = _dws.login_poll()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    if info.pop("authenticated", False):
        _store.save({})          # 触发一次落盘，保证目录存在
        _service.start()
    return jsonify({"ok": True, **info})


# ---------------- 群 ----------------
@bp.get("/api/dingtalk/groups")
def groups():
    try:
        return jsonify({"ok": True, "groups": _dws.groups()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------- 测试 ----------------
@bp.post("/api/dingtalk/test-card")
def test_card():
    cfg = _store.settings()
    if not cfg.card_template_id:
        return jsonify({"ok": False, "error": "还没配置卡片模板 ID"}), 400
    target = (request.get_json(silent=True) or {}).get("staff_id") or cfg.card_target_staff_id
    if not target:
        return jsonify({"ok": False, "error": "请先填 staffId 或群 ID"}), 400
    try:
        from .card_api import CardAPI
        from .dingtalk_api import DingTalkAPI
        api = DingTalkAPI(cfg.client_id, cfg.client_secret, cfg.robot_code)
        tid = CardAPI(api).send(
            cfg.card_template_id,
            {"title": "打印测试", "meta": "来自打印应用的测试卡片",
             "content": "如果你看到这张卡片，说明卡片链路是通的。",
             "progress": 0, "btnPrint": "🖨 打印", "btnCancel": "取消"},
            conversation_type="1", conversation_id=target, staff_id=target,
            robot_code=cfg.robot_code,
        )
        _service.log_event("info", f"测试卡片已发送 {tid}")
        return jsonify({"ok": True, "outTrackId": tid})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
