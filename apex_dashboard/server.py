#!/usr/bin/env python3
"""
خادم لوحة APEX — مكتبة قياسية فقط، GET فقط، ويستمع على 127.0.0.1 افتراضياً.

المسارات:
  GET /                          الواجهة
  GET /static/<file>             ملفات الواجهة
  GET /api/status                الوضع، Regime، رأس المال، P&L، أقصى تراجع، العدّادات
  GET /api/positions[?status=]   كل المراكز الورقية (OPEN/PENDING/CLOSED/EXPIRED)
  GET /api/positions/<id>        مركز واحد مع Execution Timeline
  GET /api/signals[?limit=]      الإشارات المسجّلة
  GET /api/chart/<SYMBOL>[?position_id=]  شموع + Volume + SMA/EMA + RSI + المستويات والعلامات
  GET /api/performance           إحصاءات Forward Test ومنحنى رأس المال

أي طريقة غير GET/HEAD ترد 405: لا مسار في هذا الخادم يكتب شيئاً.
"""
import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from apex_dashboard.api import DashboardError, DashboardReader

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _json_default(o):
    return str(o)


class DashboardHandler(BaseHTTPRequestHandler):
    reader: DashboardReader = None      # يُضبط في make_server
    server_version = "APEXDashboard/1.0"

    def log_message(self, fmt, *args):   # لا ضجيج في كونسول الحلقة
        pass

    # ── ردود ──
    def _send(self, status: int, body: bytes, ctype: str, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status: int, payload, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", head_only)

    def _refuse(self) -> None:
        self._json(405, {"error": "اللوحة للقراءة فقط — GET فقط"})

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._static("index.html", head_only)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], head_only)
            payload = self._route(path, q)
            if payload is None:
                return self._json(404, {"error": "مسار غير معروف"}, head_only)
            return self._json(200, payload, head_only)
        except DashboardError as e:
            return self._json(e.status, {"error": str(e)}, head_only)
        except ValueError as e:
            return self._json(400, {"error": f"قيمة غير صالحة: {e}"}, head_only)
        except Exception as e:  # خطأ غير متوقع لا يُسقط الخادم
            return self._json(500, {"error": f"{type(e).__name__}: {e}"}, head_only)

    def _route(self, path: str, q: dict):
        r = self.reader

        def arg(name: str) -> Optional[str]:
            v = q.get(name)
            return v[0] if v else None

        if path == "/api/status":
            return r.status()
        if path == "/api/positions":
            return {"positions": r.positions(arg("status"))}
        if path.startswith("/api/positions/"):
            return r.position(int(path.rsplit("/", 1)[1]))
        if path == "/api/signals":
            return {"signals": r.signals(int(arg("limit") or 100))}
        if path.startswith("/api/chart/"):
            pid = arg("position_id")
            return r.chart(path.rsplit("/", 1)[1], int(pid) if pid else None)
        if path == "/api/performance":
            return r.performance()
        return None

    def _static(self, name: str, head_only: bool) -> None:
        full = os.path.realpath(os.path.join(STATIC_DIR, name))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
            return self._json(404, {"error": "غير موجود"}, head_only)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype, head_only)


def make_server(reader: DashboardReader, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"reader": reader})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv
