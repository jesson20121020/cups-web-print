"""paths.py — 统一解析「应用数据目录」，保证覆盖安装/升级后数据不丢。

为什么需要它（2026-11-24 真实事故）：
    上传文件、预览图、打印任务记录原来都放在**应用安装目录**里
    （<server>/uploads、<server>/previews）。fnOS 覆盖安装会整包替换安装目录，
    这些数据就跟着没了；而且不同启动路径带的变量不一样（有的只有 DATA_DIR、
    没有 TRIM_PKGVAR），导致 Python 侧和 cmd/main 侧算出的数据目录还可能不一致。

解析顺序（与 cmd/_datadir 保持一致）：
    1. APP_DATA_DIR / DATA_DIR（应用启动脚本导出，首选）
    2. TRIM_PKGVAR（fnOS 注入的应用数据根）
    3. 从安装目录的 home 软链推导：<vol>/@appdata/<appname>
    4. 兜底：<安装目录>/var
"""
import os

# 本文件所在目录 = 应用代码目录（<app>/server 或 <app>/target/server）
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

# 兼容旧布局：老版本把数据写在代码目录旁，迁移时要用
LEGACY_UPLOADS = os.path.join(SERVER_DIR, "uploads")
LEGACY_PREVIEWS = os.path.join(SERVER_DIR, "previews")


def _app_install_dir() -> str:
    """往上找带 home 软链的安装目录；找不到就返回代码目录的上一级。"""
    cur = SERVER_DIR
    for _ in range(4):
        parent = os.path.dirname(cur)
        if parent in ("", "/"):
            break
        if os.path.islink(os.path.join(parent, "home")):
            return parent
        cur = parent
    return os.path.dirname(SERVER_DIR)


def _app_name(install_dir: str) -> str:
    name = os.environ.get("TRIM_APPNAME") or ""
    if name:
        return name
    # /var/apps/<app>/target/server -> <app>
    for cand in (install_dir, os.path.dirname(install_dir)):
        base = os.path.basename(cand.rstrip("/"))
        if base and base not in ("target", "server"):
            return base
    return os.path.basename(install_dir.rstrip("/")) or "cups-web-print"


def app_data_dir() -> str:
    for env_name in ("APP_DATA_DIR", "DATA_DIR"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return os.path.abspath(value)

    pkgvar = (os.environ.get("TRIM_PKGVAR") or "").strip()
    if pkgvar:
        return os.path.abspath(pkgvar)

    install_dir = _app_install_dir()
    home_link = os.path.join(install_dir, "home")
    if os.path.islink(home_link):
        home_real = os.path.realpath(home_link)          # /volX/@apphome/<app>
        parts = home_real.split(os.sep)
        if len(parts) > 2 and parts[1].startswith("@"):
            vol = os.sep + parts[1]                      # /volX
            return os.path.join(vol, "@appdata", _app_name(install_dir))
    return os.path.join(install_dir, "var")


APP_DATA_DIR = app_data_dir()
UPLOADS_DIR = os.path.join(APP_DATA_DIR, "uploads")
PREVIEWS_DIR = os.path.join(APP_DATA_DIR, "previews")
JOBS_FILE = os.path.join(APP_DATA_DIR, "jobs.json")


def ensure_dirs() -> None:
    for d in (APP_DATA_DIR, UPLOADS_DIR, PREVIEWS_DIR):
        os.makedirs(d, exist_ok=True)


def migrate_legacy_dirs(log=None) -> list:
    """把老版本留在安装目录里的 uploads/previews 搬到数据目录（一次性、不删源文件）。

    覆盖安装会替换安装目录，这些老文件如果不搬走就彻底没了。
    """
    moved = []
    for legacy, target in ((LEGACY_UPLOADS, UPLOADS_DIR), (LEGACY_PREVIEWS, PREVIEWS_DIR)):
        try:
            if not os.path.isdir(legacy):
                continue
            if os.path.abspath(legacy) == os.path.abspath(target):
                continue
            os.makedirs(target, exist_ok=True)
            count = 0
            for name in os.listdir(legacy):
                src = os.path.join(legacy, name)
                dst = os.path.join(target, name)
                if not os.path.isfile(src) or os.path.exists(dst):
                    continue
                try:
                    os.replace(src, dst)      # 同卷直接 rename，跨卷自动退化为拷贝
                except OSError:
                    import shutil
                    shutil.copy2(src, dst)
                count += 1
            if count:
                moved.append((legacy, target, count))
                if log:
                    log.info("数据迁移：%s -> %s（%d 个文件）", legacy, target, count)
        except Exception as exc:              # 迁移失败不能影响启动
            if log:
                log.warning("数据迁移失败 %s -> %s: %s", legacy, target, exc)
    return moved


def restore_pending_archive(log=None) -> bool:
    """启动时应用一次性数据恢复包（restore-pending*.tar.gz 放在数据目录里）。

    用途：fnOS 覆盖安装走的是「卸载(保留数据) + 重新安装」，而老版本的用户文件
    （uploads/previews）原本在**安装目录**里，卸载时会被一起删掉。升级前把它们
    打成 restore-pending.tar.gz 放进数据目录，首次启动自动解回来。
    可能有多份（重复上传会变成 restore-pending_1.tar.gz），按修改时间**从新到旧**
    全部应用，解压完改名 *.done，避免重复执行。
    """
    import glob as _glob
    import tarfile

    paths = [p for p in _glob.glob(os.path.join(APP_DATA_DIR, "restore-pending*.tar.gz"))
             if not p.endswith(".done")]
    if not paths:
        return False
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)   # 新的先解，旧的只补缺
    applied = 0
    for path in paths:
        try:
            with tarfile.open(path, "r:gz") as tf:
                members = [m for m in tf.getmembers()
                           if not (m.name.startswith("/") or ".." in m.name.split("/"))]
                # 保留已存在的文件（新的恢复包优先），只补缺失的
                skip = set()
                for m in members:
                    target = os.path.join(APP_DATA_DIR, m.name)
                    if os.path.exists(target) and not m.isdir():
                        skip.add(m.name)
                todo = [m for m in members if m.name not in skip]
                try:
                    tf.extractall(APP_DATA_DIR, members=todo, filter="data")   # py>=3.12
                except TypeError:
                    tf.extractall(APP_DATA_DIR, members=todo)
            os.replace(path, path + ".done")
            applied += 1
            if log:
                log.info("已从 %s 恢复 %d 项（跳过已存在 %d 项）",
                         os.path.basename(path), len(todo), len(skip))
        except Exception as exc:
            if log:
                log.warning("恢复数据包失败 %s: %s", path, exc)
    return applied > 0
