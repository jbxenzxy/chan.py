# -*- coding: utf-8 -*-
"""「保存到自选」真浏览器 E2E 的打桩后端（stdlib，零依赖）。

只做两件事：
  1. 把 `Frontend/` 原样当静态站点发出去（**真 app.html + 真 app.js**）；
  2. 把 `/api/*` 全部打桩，`/api/stocks/scan/save/zxg` 按请求里的 codes 决定
     行为（成功 / 500 / 慢响应），从而在真浏览器里复现「慢响应触发 4s 提示」
     这类**渲染时序**——那是 `Test/test_scan_panel_collapse.py` 的 node 桩
     （fake timer）测不到的：桩里时钟是假的，不会真的走到 4000ms 那一帧。

行为开关（由 codes 内容决定，服务无状态、可并发）：
  · 含 `SLOW`   → 延迟 5.2s 再回（> 前端 4000ms 的 slowTimer）
  · 含 `FAILME` → HTTP 500，前端走 catch 分支
  · 其余        → 立即回 `{"tdx_saved": n, "ths_saved": n, "ths_msg": "ok"}`

请求记录只放内存，通过 `GET /__requests` 取出（供 E2E 断言"到底发了几次、
参数对不对"）。
"""
import json
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "Frontend")

SAVE_PATH = "/api/stocks/scan/save/zxg"
SLOW_DELAY = 5.2
REQUESTS = []
_LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=FRONTEND, **kw)

    # 静音访问日志（E2E 只看断言，不看 access log）
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/__requests":
            with _LOCK:
                return self._json(list(REQUESTS))
        if parsed.path.startswith("/api/"):
            if parsed.path == "/api/config":
                # 只给扫描面板关心的三项；其余字段缺失由 app.js 的 if 保护兜住
                return self._json({"config": {
                    "view_count": 500,
                    "scan_min_float_mc": 30,
                    "scan_fangliang_window_bars": 20,
                }})
            # 首屏 `initDefault()` 会拉默认标的 K 线，缺 `meta` 会直接抛
            # 「首屏数据加载失败」，把整个 app.js 初始化打断——真浏览器里这是
            # 硬失败（node 桩不会跑到这里），所以必须给一个形状完整的空图。
            if parsed.path.startswith("/api/stocks/") and parsed.path.endswith("/analyze"):
                # klines 不能为空：`initDefault` 会取 `klines[length-1].date` 填
                # 「跳转日期」输入框，空数组直接 TypeError，整个 app.js 初始化中断。
                return self._json({
                    "meta": {"symbol": "sh000001", "name": "上证指数", "freq": "日线",
                             "start_date": "2024-01-02", "end_date": "2024-01-03"},
                    "klines": [
                        {"date": "2024-01-02", "open": 2972.0, "high": 2985.0,
                         "low": 2962.0, "close": 2980.0, "volume": 1000000},
                        {"date": "2024-01-03", "open": 2980.0, "high": 2990.0,
                         "low": 2975.0, "close": 2985.0, "volume": 1200000},
                    ],
                    # 复数键名（bis/fxs）是 `updateSlider` 直接 `.filter()` 的字段，
                    # 少一个就 TypeError；单数键（bi/seg）另有用处，一并给空数组。
                    "bis": [], "fxs": [], "bi": [], "seg": [], "zs": [], "bsp": [],
                    "signals": [], "stats": {},
                })
            return self._json({})
        if parsed.path == "/":
            self.path = "/app.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != SAVE_PATH:
            return self._json({})
        qs = parse_qs(parsed.query)
        codes = (qs.get("codes") or [""])[0]
        items = [c for c in codes.split(",") if c]
        with _LOCK:
            REQUESTS.append({"codes": items, "at": time.time()})
        if any("FAILME" in c for c in items):
            return self._json({"error": "stub failure"}, status=500)
        if any("SLOW" in c for c in items):
            time.sleep(SLOW_DELAY)
        n = len(items)
        return self._json({"tdx_saved": n, "ths_saved": n, "ths_msg": "ok"})


class QuietServer(ThreadingHTTPServer):
    """浏览器关页时连接会被 RST，socketserver 默认把整段 traceback 打到 stderr。

    E2E 的输出只应包含断言结果，这些噪音会把真正的失败埋掉，故静音。
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def serve(port=0):
    httpd = QuietServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]
