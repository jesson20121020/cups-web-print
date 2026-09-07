#!/usr/bin/env python3
"""cups-web-print 飞牛统一网关代理.

监听 Unix socket (${TRIM_APPDEST}/target/app.sock, 由 fnOS 统一网关转发),
把 HTTP 请求反向代理到 127.0.0.1:CUPS_WEB_PORT (Flask).

复用标准库 http.server + http.client, 无第三方依赖.

用法:
    gateway.py <sock_path> <backend_host> <backend_port>

环境变量:
    GATEWAY_SOCK  - Unix socket 路径 (默认 /var/apps/cups-web-print/target/app.sock)
    BACKEND_HOST   - 后端主机 (默认 127.0.0.1)
    BACKEND_PORT   - 后端端口 (默认 5000)
"""
import http.client
import os
import socket
import socketserver
import sys
import threading


def _hop_by_hop(headers):
    """移除 hop-by-hop 头 (Connection / Keep-Alive / Proxy-Authenticate / Proxy-Authorization
    / TE / Trailers / Transfer-Encoding / Upgrade).
    HTTP/1.1 代理转发规则要求去掉这些, 否则会破坏转发语义."""
    drop = {
        "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade",
    }
    return [(k, v) for k, v in headers if k.lower() not in drop]


class GatewayHandler(socketserver.BaseRequestHandler):
    """每个连接一个 HTTP 请求 (短连接). Flask 不需要 WebSocket, 够用."""

    def handle(self):
        try:
            self._serve()
        except Exception as e:
            try:
                self._send_error(e)
            except Exception:
                pass

    def _serve(self):
        # 读请求行
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(65536)
            if not chunk:
                return
            data += chunk
            if len(data) > 1024 * 1024:
                return  # 单请求头超过 1MB 直接放弃

        header_end = data.index(b"\r\n\r\n")
        head = data[:header_end].decode("iso-8859-1", errors="replace")
        body = data[header_end + 4:]

        lines = head.split("\r\n")
        if not lines:
            return
        request_line = lines[0]
        parts = request_line.split(" ")
        if len(parts) != 3:
            self._send_raw(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        method, path, _ = parts

        # 解析 header
        headers = []
        content_length = 0
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                headers.append((k, v))
                if k.lower() == "content-length":
                    try:
                        content_length = int(v)
                    except ValueError:
                        content_length = 0

        # 把残余 body 读完
        while len(body) < content_length:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            body += chunk

        # 连接后端
        backend_host = self.server.backend_host
        backend_port = self.server.backend_port
        conn = http.client.HTTPConnection(backend_host, backend_port, timeout=300)

        # Host 头重写: Flask 端可能校验 Host; 我们重写为 127.0.0.1:port
        forwarded_headers = _hop_by_hop(headers)
        # 去掉 Host (http.client 会自己加)
        forwarded_headers = [(k, v) for k, v in forwarded_headers if k.lower() != "host"]

        # 剥掉 gatewayPrefix 前缀（例如 /app/cups-web-print），让 Flask 看到干净的路径
        # Flask 路由如 "/"、" /api/..."、" /static/..." 都是不带前缀的
        prefix = self.server.gateway_prefix
        backend_path = path
        if prefix and (path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?")):
            backend_path = path[len(prefix):]
            if not backend_path:
                backend_path = "/"
            # 关键修复：如果 backend_path 不以 "/" 开头（如 "?path=..."）
            # Flask 收到 "/?path=..." 是合法 200，但如果收到 "?path=..."（无前导 /）
            # Flask 会 308 redirect 到 127.0.0.1:5000/?path=...，外网客户端访问失败。
            # 我们在 gateway 这一层就补上前导 "/"。
            if not backend_path.startswith("/"):
                backend_path = "/" + backend_path

        try:
            # http.client.headers 需要 dict，不能直接传 list of tuples
            headers_dict = {}
            for k, v in forwarded_headers:
                if k in headers_dict:
                    headers_dict[k] = f"{headers_dict[k]}, {v}"
                else:
                    headers_dict[k] = v
            conn.request(method, backend_path, body=body, headers=headers_dict)
            resp = conn.getresponse()

            # 读 response body
            resp_body = resp.read()

            # 构造响应
            resp_headers = _hop_by_hop(resp.getheaders())
            status_line = f"HTTP/1.1 {resp.status} {resp.reason}\r\n"

            out = status_line.encode("ascii")
            for k, v in resp_headers:
                out += f"{k}: {v}\r\n".encode("iso-8859-1")
            out += b"\r\n"
            out += resp_body

            self.request.sendall(out)
        finally:
            conn.close()

    def _send_raw(self, payload: bytes):
        try:
            self.request.sendall(payload)
        except Exception:
            pass

    def _send_error(self, exc):
        msg = f"Gateway error: {exc}".encode("utf-8", errors="replace")
        payload = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: " + str(len(msg)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + msg
        )
        self._send_raw(payload)


class UnixSocketHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Unix socket 上的 HTTP server, 每连接一个线程."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sock_path: str, backend_host: str, backend_port: int,
                 gateway_prefix: str = ""):
        # 删除旧 socket
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        # 确保父目录存在
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.gateway_prefix = gateway_prefix
        super().__init__(sock_path, GatewayHandler)


def resolve_socket_path(candidate: str) -> str:
    """解析 socket 路径——把任何形式的 candidate 统一到 fnOS 网关能识别的真实路径.

    fnOS 网关按 gatewaySocket='app.sock' 在 ${TRIM_APPDEST}/<gatewaySocket> 找 socket,
    即 /var/apps/<app>/app.sock, 跟随 ${TRIM_APPDEST}/target 软链到
    /vol{n}/@appcenter/<app>/app.sock (注意: 没有 target/ 子目录).

    candidate 可能的形式:
      1. /var/apps/<app>/target/app.sock          (cmd/main 用 readlink -f 前的形式)
      2. /var/apps/<app>/app.sock                  (fnOS 网关直接找的形式)
      3. /vol{n}/@appcenter/<app>/target/app.sock  (cmd/main 错算的 target/app.sock)
      4. /vol{n}/@appcenter/<app>/app.sock         (fnOS 网关最终跟随软链解析)

    全部统一到形式 4.
    """
    norm = os.path.realpath(candidate)  # 跟随软链

    # 形式 1: /var/apps/<app>/target/app.sock  (target 是软链)
    parts = candidate.split("/")
    if (len(parts) >= 5 and parts[1] == "var" and parts[2] == "apps"
            and parts[4] == "target"):
        appname = parts[3]
        rest = "/".join(parts[5:]) if len(parts) > 5 else ""
        for vol in ["/vol1", "/vol2", "/vol3", "/vol4", "/vol5", "/vol6"]:
            root = f"{vol}/@appcenter/{appname}"
            if os.path.isdir(root):
                resolved = f"{root}/{rest}" if rest else root
                os.makedirs(os.path.dirname(resolved), exist_ok=True)
                return resolved

    # 形式 2: /var/apps/<app>/app.sock  (target 未参与)
    if (len(parts) >= 5 and parts[1] == "var" and parts[2] == "apps"):
        appname = parts[3]
        rest = "/".join(parts[4:]) if len(parts) > 4 else ""
        for vol in ["/vol1", "/vol2", "/vol3", "/vol4", "/vol5", "/vol6"]:
            root = f"{vol}/@appcenter/{appname}"
            if os.path.isdir(root):
                resolved = f"{root}/{rest}" if rest else root
                os.makedirs(os.path.dirname(resolved), exist_ok=True)
                return resolved

    # 形式 3 或 4: /vol{n}/@appcenter/<app>/...app.sock
    # 如果路径里有 /@appcenter/<app>/target/, 剥掉 target/
    for vol in ["/vol1", "/vol2", "/vol3", "/vol4", "/vol5", "/vol6"]:
        prefix = f"{vol}/@appcenter/"
        if norm.startswith(prefix):
            rest = norm[len(prefix):]
            parts2 = rest.split("/")
            if len(parts2) >= 2 and parts2[1] == "target":
                # 剥掉 target/
                stripped = prefix + parts2[0] + "/" + "/".join(parts2[2:])
                os.makedirs(os.path.dirname(stripped), exist_ok=True)
                return stripped
            break

    # fallback: 直接用 realpath 结果
    os.makedirs(os.path.dirname(norm), exist_ok=True)
    return norm


def main():
    sock_candidate = os.environ.get(
        "GATEWAY_SOCK",
        sys.argv[1] if len(sys.argv) > 1 else "/var/apps/cups-web-print/target/app.sock",
    )
    backend_host = os.environ.get(
        "BACKEND_HOST",
        sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1",
    )
    backend_port = int(os.environ.get(
        "BACKEND_PORT",
        sys.argv[3] if len(sys.argv) > 3 else "5000",
    ))

    sock_path = resolve_socket_path(sock_candidate)

    server = UnixSocketHTTPServer(sock_path, backend_host, backend_port,
                                 gateway_prefix=os.environ.get("GATEWAY_PREFIX", "/app/cups-web-print"))
    # socket 权限: 0660 让 fnOS 网关进程能连接
    try:
        os.chmod(sock_path, 0o660)
    except Exception:
        pass

    print(f"[gateway] listening on unix://{sock_path}", flush=True)
    print(f"[gateway] proxying to http://{backend_host}:{backend_port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()