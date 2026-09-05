"""临时 SPA 服务（端口 8090）：serve 前端 dist + API 反向代理到 8080。

用途：当用户浏览器顽固缓存 8080 的旧 index.html 时，提供一个全新端口入口，
浏览器对该端口无任何历史缓存，首次请求必为最新版。serve 时动态给 HTML 内
/assets/index-*.js|css 引用注入 ?v=<dist mtime>，彻底绕开本地磁盘缓存。

同时把 /api/* 透传到 http://127.0.0.1:8080，让 8090 也能完整使用系统。
SPA fallback：未知路径回退到 index.html（前端路由刷新不 404）。
"""
from __future__ import annotations

import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# 本文件位于 <项目根>/backend/scripts/，按自身位置推导 dist，
# 不写死盘符——否则换机后这个"救急工具"自己先失效。
DIST = str(Path(__file__).resolve().parents[2] / "frontend" / "dist")
PORT = 8090
API_UPSTREAM = "http://127.0.0.1:8080"
ASSET_RE = re.compile(rb"(/assets/index-[A-Za-z0-9_-]+\.)(js|css)(?!\?)")


def _dist_mtime() -> str:
    try:
        return f"{os.path.getmtime(os.path.join(DIST, 'index.html')):.0f}"
    except Exception:
        return "0"


def _stamp_html(body: bytes) -> bytes:
    v = _dist_mtime()
    return ASSET_RE.sub(lambda m: m.group(1) + m.group(2) + f"?v={v}".encode(), body)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body: bytes, cache: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if "text/html" in ctype:
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_api(self):
        """把 /api/* 透传到 8080，保持 method/headers/body，返回响应。"""
        upstream = API_UPSTREAM + self.path
        try:
            body_len = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(body_len) if body_len else None
            req = urllib.request.Request(
                upstream,
                data=body,
                method=self.command,
                headers={k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = f'{{"detail": "API 转发失败: {e}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._proxy_api()
        if path.startswith("/assets/"):
            fp = os.path.join(DIST, path.lstrip("/"))
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                ctype = "text/javascript" if fp.endswith(".js") else (
                    "text/css" if fp.endswith(".css") else "application/octet-stream")
                self._send(200, ctype, data, "public, max-age=31536000, immutable")
                return
            self._send(404, "text/plain", b"not found", "no-cache")
            return
        idx = os.path.join(DIST, "index.html")
        if os.path.isfile(idx):
            with open(idx, "rb") as f:
                data = f.read()
            data = _stamp_html(data)
            self._send(200, "text/html; charset=utf-8", data, "no-cache, no-store, must-revalidate, max-age=0")
            return
        self._send(500, "text/plain", b"dist/index.html missing", "no-cache")

    def do_POST(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._proxy_api()
        self._send(405, "text/plain", b"method not allowed", "no-cache")

    def do_PUT(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._proxy_api()
        self._send(405, "text/plain", b"method not allowed", "no-cache")

    def do_DELETE(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._proxy_api()
        self._send(405, "text/plain", b"method not allowed", "no-cache")

    def do_OPTIONS(self):
        if urlparse(self.path).path.startswith("/api/"):
            return self._proxy_api()
        self._send(204, "text/plain", b"", "no-cache")

    def log_message(self, *a):
        pass  # 安静


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[spa_server] 监听 http://127.0.0.1:{PORT}  (serve {DIST})")
    print(f"[spa_server] /api/* 透传到 {API_UPSTREAM}")
    print("[spa_server] 动态版本戳 + 强 no-cache，专治浏览器顽固缓存旧 index.html")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
