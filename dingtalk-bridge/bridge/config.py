"""环境变量配置。所有可调项都在这里，改 .env 即可，不用动代码。"""
import os
from dataclasses import dataclass, field


def _load_dotenv() -> None:
    """本地直接跑（非 docker）时读一下 .env；已有环境变量优先。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        except OSError:
            pass
        return


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str) -> list:
    raw = _env(name)
    return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]


@dataclass
class Config:
    # --- 钉钉 ---
    client_id: str = field(default_factory=lambda: _env("DINGTALK_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _env("DINGTALK_CLIENT_SECRET"))
    robot_code: str = field(default_factory=lambda: _env("DINGTALK_ROBOT_CODE"))
    # 互动卡片模板 ID（留空则启动时自动创建并缓存）
    card_template_id: str = field(default_factory=lambda: _env("CARD_TEMPLATE_ID"))

    # 只允许这些钉钉 staffId 触发打印；为空表示不限制（不建议）
    allow_user_ids: list = field(default_factory=lambda: _env_list("ALLOW_USER_IDS"))
    # 允许触发的群（conversationId）；群里这些人 @机器人 也能打
    allow_group_ids: list = field(default_factory=lambda: _env_list("ALLOW_GROUP_IDS"))

    # --- 打印服务 ---
    print_service: str = field(default_factory=lambda: _env("PRINT_SERVICE", "http://192.168.31.190:5000").rstrip("/"))
    printer_name: str = field(default_factory=lambda: _env("PRINTER_NAME"))  # 空 = 自动挑第一台 IPP 队列
    request_timeout: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 60))

    # --- 默认打印参数（与 cups-print skill / 网页默认一致）---
    default_color: str = field(default_factory=lambda: _env("DEFAULT_COLOR", "color"))          # color | mono
    default_duplex: str = field(default_factory=lambda: _env("DEFAULT_DUPLEX", "two-sided-long-edge"))
    default_paper: str = field(default_factory=lambda: _env("DEFAULT_PAPER", "A4"))
    default_paper_type: str = field(default_factory=lambda: _env("DEFAULT_PAPER_TYPE", "plain"))
    default_orientation: str = field(default_factory=lambda: _env("DEFAULT_ORIENTATION", "portrait"))
    default_scaling: str = field(default_factory=lambda: _env("DEFAULT_SCALING", "fit"))

    # --- 行为 ---
    batch_window: int = field(default_factory=lambda: _env_int("BATCH_WINDOW_SECONDS", 8))   # 多图合并静默窗口
    pending_ttl: int = field(default_factory=lambda: _env_int("PENDING_TTL_MINUTES", 30))    # 待确认任务有效期
    job_timeout: int = field(default_factory=lambda: _env_int("JOB_TIMEOUT_SECONDS", 300))   # 等打印结果上限
    inbox_dir: str = field(default_factory=lambda: _env("INBOX_DIR", "/data/inbox"))

    # --- 群消息自动抓取（dws CLI）---
    group_enabled: bool = field(default_factory=lambda: _env("GROUP_POLL_ENABLED", "0") in ("1", "true", "yes"))
    group_name: str = field(default_factory=lambda: _env("GROUP_NAME", "木耳作业"))
    dws_bin: str = field(default_factory=lambda: _env("DWS_BIN", "dws"))
    group_poll_seconds: int = field(default_factory=lambda: _env_int("GROUP_POLL_SECONDS", 20))
    group_fetch_limit: int = field(default_factory=lambda: _env_int("GROUP_FETCH_LIMIT", 30))
    # 卡片投递到哪个单聊（机器人 staffId）；留空则投到群里
    card_target_staff_id: str = field(default_factory=lambda: _env("CARD_TARGET_STAFF_ID"))
    group_skip_history: bool = field(default_factory=lambda: _env("GROUP_SKIP_HISTORY", "1") in ("1", "true", "yes"))
    # event=事件流（默认，实时）；poll=定时轮询
    group_mode: str = field(default_factory=lambda: _env("GROUP_MODE", "event"))
    group_chat_id: str = field(default_factory=lambda: _env("GROUP_CHAT_ID"))
    # 兜底轮询间隔（秒），事件流断了也能补上
    group_catchup_seconds: int = field(default_factory=lambda: _env_int("GROUP_CATCHUP_SECONDS", 600))
    event_port: int = field(default_factory=lambda: _env_int("EVENT_PORT", 8099))

    def validate(self) -> None:
        missing = [k for k in ("client_id", "client_secret") if not getattr(self, k)]
        if missing:
            raise SystemExit(
                "缺少钉钉应用凭证：%s。请在 .env 里填 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET"
                % ", ".join(missing)
            )
        if not self.robot_code:
            # 企业内部机器人默认 robotCode == AppKey
            self.robot_code = self.client_id
