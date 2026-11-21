"""dws CLI 管理：安装、版本、设备码授权、群列表。

dws 是钉钉官方开源 CLI（Apache-2.0），这里只负责把它装到应用数据目录里，
用独立的 DWS_CONFIG_DIR / DWS_KEYCHAIN_DIR，避免污染系统目录。
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.request

LOG = logging.getLogger("dingtalk.dws")

GITHUB_API = "https://api.github.com/repos/DingTalk-Real-AI/dingtalk-workspace-cli/releases/latest"
GITEE_RELEASE = "https://gitee.com/api/v5/repos/DingTalk-Real-AI/dingtalk-workspace-cli/releases/latest"


class Dws:
    def __init__(self, data_dir: str):
        self.root = os.path.join(data_dir, "dws")
        self.bin = os.path.join(self.root, "dws")
        self.config_dir = os.path.join(self.root, "config")
        self.keychain_dir = os.path.join(self.root, "keychain")
        os.makedirs(self.root, exist_ok=True)
        self._login_proc = None
        self._login_info = {}
        self._lock = threading.Lock()

    # ---------- 基础 ----------
    def env(self) -> dict:
        e = dict(os.environ)
        e["DWS_CONFIG_DIR"] = self.config_dir
        e["DWS_KEYCHAIN_DIR"] = self.keychain_dir
        e["DWS_NO_UPDATE_CHECK"] = "1"
        return e

    def installed(self) -> bool:
        return os.path.isfile(self.bin) and os.access(self.bin, os.X_OK)

    def run(self, args: list, timeout: int = 60, cwd: str = None) -> dict:
        if not self.installed():
            raise RuntimeError("dws 未安装")
        out = subprocess.run([self.bin] + args, capture_output=True, text=True,
                             timeout=timeout, env=self.env(), cwd=cwd or self.root)
        text = out.stdout.strip()
        start = text.find("{")
        if start < 0:
            raise RuntimeError((out.stderr or text or "dws 无输出")[:300])
        return json.loads(text[start:])

    def version(self) -> str:
        if not self.installed():
            return ""
        try:
            out = subprocess.run([self.bin, "version"], capture_output=True, text=True,
                                 timeout=20, env=self.env())
            m = re.search(r"Version:\s*(\S+)", out.stdout or "")
            return m.group(1) if m else ""
        except Exception:
            return ""

    # ---------- 安装 ----------
    def install(self, mirror: str = "github", url: str = None) -> dict:
        os.makedirs(self.root, exist_ok=True)
        if not url:
            url = self._latest_tarball_url(mirror)
        LOG.info("下载 dws：%s", url)
        tmp = os.path.join(self.root, "dws.tar.gz")
        urllib.request.urlretrieve(url, tmp)
        with tarfile.open(tmp, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/dws") or member.name == "dws":
                    member.name = "dws"
                    tar.extract(member, self.root)
                    break
        os.chmod(self.bin, 0o755)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        ver = self.version()
        LOG.info("dws 安装完成：%s", ver)
        return {"ok": True, "version": ver}

    @staticmethod
    def _latest_tarball_url(mirror: str = "github") -> str:
        import platform
        arch = "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64"
        name = f"dws-linux-{arch}.tar.gz"
        if mirror == "gitee":
            data = json.loads(urllib.request.urlopen(GITEE_RELEASE, timeout=30).read())
            for asset in data.get("assets", []):
                if asset.get("name") == name:
                    return asset["browser_download_url"]
        req = urllib.request.Request(GITHUB_API, headers={"User-Agent": "cups-web-print"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        for asset in data.get("assets", []):
            if asset.get("name") == name:
                return asset["browser_download_url"]
        raise RuntimeError(f"没找到发布包 {name}")

    # ---------- 授权 ----------
    def auth_status(self) -> dict:
        if not self.installed():
            return {"authenticated": False, "message": "dws 未安装"}
        try:
            out = subprocess.run([self.bin, "auth", "status", "--format", "json"],
                                 capture_output=True, text=True, timeout=30, env=self.env())
            data = json.loads((out.stdout or "{}").strip() or "{}")
            return {"authenticated": bool(data.get("authenticated")),
                    "user_name": data.get("user_name", ""),
                    "corp_name": data.get("corp_name", ""),
                    "message": data.get("message", "")}
        except Exception as exc:
            return {"authenticated": False, "message": str(exc)}

    def login_start(self) -> dict:
        """启动设备码登录，返回授权码和链接；进程后台等待扫码。"""
        with self._lock:
            if not self.installed():
                raise RuntimeError("dws 未安装")
            if self._login_proc and self._login_proc.poll() is None:
                return dict(self._login_info)
            log_path = os.path.join(self.root, "login.log")
            log = open(log_path, "w", encoding="utf-8")
            self._login_proc = subprocess.Popen(
                [self.bin, "auth", "login", "--device"],
                stdout=log, stderr=subprocess.STDOUT, env=self.env(), cwd=self.root)
            self._login_info = {"code": "", "url": "", "started": True}

            def _watch():
                import time
                for _ in range(40):          # 最多等 40 秒拿到授权码
                    time.sleep(1)
                    try:
                        text = open(log_path, encoding="utf-8").read()
                    except OSError:
                        continue
                    code = re.search(r"授权码[:：]\s*([A-Z0-9-]+)", text)
                    url = re.search(r"(https://login\.dingtalk\.com/oauth2/device/verify\.htm\S+)", text)
                    if url and not self._login_info.get("url"):
                        self._login_info.update(code=code.group(1) if code else "", url=url.group(1))
                        return
            threading.Thread(target=_watch, daemon=True).start()
            import time
            for _ in range(25):
                if self._login_info.get("url"):
                    break
                time.sleep(1)
            return dict(self._login_info)

    def login_poll(self) -> dict:
        status = self.auth_status()
        proc_alive = bool(self._login_proc and self._login_proc.poll() is None)
        return {"authenticated": status.get("authenticated", False),
                "user_name": status.get("user_name", ""),
                "corp_name": status.get("corp_name", ""),
                "message": status.get("message", ""),
                "pending": proc_alive}

    # ---------- 群 ----------
    def groups(self) -> list:
        data = self.run(["chat", "+my-groups", "--page-all", "-f", "json"], timeout=60)
        return [{"id": g.get("conversationId", ""), "name": g.get("name", ""),
                 "members": g.get("memberCount", 0)} for g in data.get("groups", [])]
