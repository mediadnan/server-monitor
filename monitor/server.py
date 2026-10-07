"""HTTP server: the dashboard page, its static files and a small JSON API.

GET /                  dashboard
GET /static/<file>     CSS / JS / icon
GET /api/now           latest values      {ts, rev, v, notes, info}
GET /api/tiles         tile definitions   {rev, title, groups, tiles}
GET /api/history       ?tier=fast|slow    {t, series}
GET /healthz           "ok" (no auth, no data; for container health checks)
"""

import base64
import hmac
import html
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
STATIC = {   # explicit allow-list: nothing else on disk is ever served
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def load_static():
    files = {}
    for name in STATIC:
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            files[name] = f.read()
    return files


def make_handler(monitor, cfg):
    static = load_static()
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read().replace("{{TITLE}}", html.escape(cfg.title)).encode()

    expected = None
    if cfg.auth_password:
        token = f"{cfg.auth_user}:{cfg.auth_password}".encode()
        expected = b"Basic " + base64.b64encode(token)

    class Handler(BaseHTTPRequestHandler):
        server_version = "server-monitor"
        sys_version = ""

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self.reply(200, b"ok", "text/plain; charset=utf-8")
            if not self.authorized():
                return
            if url.path == "/":
                return self.reply(200, page, "text/html; charset=utf-8")
            if url.path.startswith("/static/"):
                name = url.path[len("/static/"):]
                if name in static:
                    return self.reply(200, static[name], STATIC[name])
            elif url.path == "/api/now":
                return self.json(monitor.now())
            elif url.path == "/api/tiles":
                return self.json(monitor.tile_list())
            elif url.path == "/api/history":
                tier = parse_qs(url.query).get("tier", ["fast"])[0]
                return self.json(monitor.history_columns(
                    "slow" if tier == "slow" else "fast"))
            self.reply(404, b"not found", "text/plain; charset=utf-8")

        def authorized(self):
            if expected is None:
                return True
            got = (self.headers.get("Authorization") or "").encode()
            if hmac.compare_digest(got, expected):
                return True
            if got:
                time.sleep(1)   # slow down password guessing
            self.reply(401, b"authentication required", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="Server Monitor", charset="UTF-8"'})
            return False

        def json(self, data):
            self.reply(200, json.dumps(data, separators=(",", ":")).encode(),
                       "application/json")

        def reply(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass   # polled every 2 s; access logs would only be noise

    return Handler


def serve(monitor, cfg):
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(monitor, cfg))
    httpd.daemon_threads = True
    log.info("listening on :%d (auth %s)", cfg.port,
             "on" if cfg.auth_password else "OFF")
    return httpd
