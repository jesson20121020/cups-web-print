"""接收宿主机 dws 事件流中继过来的消息 ID，塞进打印流水线。

为什么不用容器自己跑 dws event：
  容器的精简环境里 dws 的本地 bus 子进程起不来（bus child reported startup failure），
  宿主机上正常。所以事件流跑在宿主机，通过 HTTP 把 message_id 推过来。

POST /event  {"message_ids": ["msg..."]}
POST /ping   -> {"ok": true}
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("bridge.inbox")


_target = {"watcher": None, "started": False}
_lock = threading.Lock()


def set_target(watcher) -> None:
    _target["watcher"] = watcher


def start_once(port: int = 8099):
    """整个进程只起一个 HTTP 入口；重启服务只换 target。"""
    with _lock:
        if _target["started"]:
            return None
        _target["started"] = True
    return start(port)


def start(port: int = 8099) -> threading.Thread:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静点
            LOG.debug("inbox %s", fmt % args)

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})

            if self.path.startswith("/ping"):
                return self._json(200, {"ok": True})

            if self.path.startswith("/event"):
                ids = data.get("message_ids") or ([data["message_id"]] if data.get("message_id") else [])
                if not ids:
                    return self._json(400, {"ok": False, "error": "no message_ids"})
                w = _target.get("watcher")
                if w is None:
                    return self._json(503, {"ok": False, "error": "service not running"})
                w.submit(ids)
                LOG.info("收到事件中继：%d 条消息", len(ids))
                return self._json(200, {"ok": True, "queued": len(ids)})

            self._json(404, {"ok": False})

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    LOG.info("事件中继入口已监听 :%d", port)
    return t
