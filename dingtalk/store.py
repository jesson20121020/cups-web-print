"""钉钉打印集成：配置存储。

配置落盘到 JSON（默认 <app>/dingtalk-data/config.json，权限 600），
Web 页面读写它；后台服务启动时读同一份。
"""
import json
import logging
import os
import threading

LOG = logging.getLogger("dingtalk.store")

# 数据目录优先级：显式环境变量 > fnOS 应用数据目录(TRIM_PKGVAR) > 代码目录旁
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.environ.get("DINGTALK_DATA_DIR") or os.path.join(
    os.environ.get("TRIM_PKGVAR") or APP_DIR, "dingtalk-data")

DEFAULTS = {
    # 钉钉应用
    "client_id": "",
    "client_secret": "",
    "robot_code": "",
    "card_template_id": "",
    # 谁能用
    "allow_user_ids": [],
    "allow_group_ids": [],
    # 打印
    "print_service": "http://127.0.0.1:5000",
    "printer_name": "",
    "default_color": "color",
    "default_duplex": "one-sided",
    "default_paper": "A4",
    "default_paper_type": "plain",
    "default_scaling": "fit",
    # 群监听
    "group_enabled": False,
    "group_name": "",
    "group_chat_id": "",
    "group_mode": "event",          # poll | relay（relay 需要宿主机中继）
    "group_poll_seconds": 120,
    "group_fetch_limit": 30,
    "group_skip_history": True,
    "card_target_staff_id": "",     # 空 = 发到群里
    "preview_sheet": True,          # 卡片外再发一张「打印预览」长图
    "event_port": 8099,
    # 行为
    "batch_window": 8,
    "pending_ttl": 30,
    "job_timeout": 300,
}


class Settings:
    """把 JSON 配置包装成和原来 env 版 Config 一样的属性访问。"""

    def __init__(self, data: dict, data_dir: str):
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in (data or {}).items() if k in DEFAULTS})
        self._raw = merged
        self.data_dir = data_dir
        self.inbox_dir = os.path.join(data_dir, "inbox")
        self.request_timeout = 60
        self.dws_bin = os.path.join(data_dir, "dws", "dws")
        self.dws_config_dir = os.path.join(data_dir, "dws", "config")
        self.dws_keychain_dir = os.path.join(data_dir, "dws", "keychain")
        # 常量
        self.default_orientation = "portrait"
        self.group_catchup_seconds = 600
        self.__dict__["allow_user_ids"] = self._as_list(merged.get("allow_user_ids"))
        self.__dict__["allow_group_ids"] = self._as_list(merged.get("allow_group_ids"))

    @staticmethod
    def _as_list(v) -> list:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return [x.strip() for x in str(v or "").replace("，", ",").split(",") if x.strip()]

    def __getattr__(self, name):
        if name in self._raw:
            return self._raw[name]
        raise AttributeError(name)

    def to_dict(self) -> dict:
        d = dict(self._raw)
        d["allow_user_ids"] = self.allow_user_ids
        d["allow_group_ids"] = self.allow_group_ids
        return d

    def validate(self) -> None:
        if not self.client_id or not self.client_secret:
            raise SystemExit("缺少钉钉应用凭证（client_id / client_secret）")
        if not self.robot_code:
            self._raw["robot_code"] = self.client_id
        os.makedirs(self.inbox_dir, exist_ok=True)


class Store:
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or os.environ.get("DINGTALK_DATA_DIR") or DEFAULT_DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self.path = os.path.join(self.data_dir, "config.json")
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            LOG.warning("读取配置失败：%s", exc)
            return {}

    def save(self, data: dict) -> dict:
        with self._lock:
            # 以现有配置为底，只覆盖前端提交的字段（避免部分更新把其他项重置成默认值）
            merged = dict(DEFAULTS)
            merged.update(self.load())
            for key, value in (data or {}).items():
                if key.startswith("_"):
                    continue
                if key in ("client_secret", "card_template_id") and not value:
                    continue          # 留空表示不修改
                merged[key] = value
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return merged

    def settings(self) -> Settings:
        return Settings(self.load(), self.data_dir)

    def public(self) -> dict:
        """给前端的安全视图（敏感字段脱敏）。"""
        d = self.load()
        out = dict(DEFAULTS)
        out.update(d)
        secret = out.get("client_secret") or ""
        out["client_secret"] = ("*" * 8 + secret[-4:]) if secret else ""
        out["has_secret"] = bool(secret)
        out["_data_dir"] = self.data_dir
        return out


store = Store()
