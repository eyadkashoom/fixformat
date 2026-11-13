#!/usr/bin/env python3
"""
fixformat v7.0.2 — local web GUI (stdlib only, no tkinter).
Serves a single-page interface at http://localhost:8787/
and proxies scan() calls to the existing v7 backend modules.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: add repo root + v7/ to sys.path so we can import v7 modules
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "v7"))

# ---------------------------------------------------------------------------
# Runtime state (mutable shared dict)
# ---------------------------------------------------------------------------
STATE = {
    "running": False,
    "target": "",
    "started_at": None,
    "progress_pct": 0.0,
    "status_line": "idle",
    "hits": [],
    "log": [],
    "error": None,
}

# ---------------------------------------------------------------------------
# HTML interface (embedded so the GUI is one runnable file + this .py)
# ---------------------------------------------------------------------------
def _load_html():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_web.html")
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return "<html><body>gui_web.html missing</body></html>"

_HTML = _load_html()




# ---------------------------------------------------------------------------
# Backend scan runner (runs in a worker thread, uses v7 modules)
# ---------------------------------------------------------------------------
def _run_scan(target: str, patterns: str, chunk_size: int, results_path: str,
              carve_before: int = 4096, carve_after: int = 65536,
              carve_limit: int = 100, min_gap: int = 1048576,
              start_offset: int = 0, end_offset = None, deadline = None):
    try:
        import raw_scan
        import progress_indicators as k5

        STATE["running"] = True
        STATE["target"] = target
        STATE["started_at"] = time.time()
        STATE["progress_pct"] = 0.0
        STATE["status_line"] = "initializing…"
        STATE["hits"] = []
        STATE["error"] = None

        def _progress(**kw):
            current = kw.get("current", 0)
            total = kw.get("total", 0)
            phase = kw.get("phase", "")
            hit = kw.get("hit")
            if total:
                pct = float(current) / float(total) * 100.0
                STATE["progress_pct"] = max(STATE["progress_pct"], min(100.0, pct))
            STATE["status_line"] = f"{phase} {current}/{total}"
            if hit is not None:
                STATE["hits"].append({
                    "offset": getattr(hit, "offset", None),
                    "sig": getattr(hit, "signature", getattr(hit, "name", str(hit))),
                })

        cb = _progress
        raw_scan.scan(
            target,
            patterns=patterns,
            chunk_size=chunk_size,
            results_path=results_path,
            carve_before=carve_before,
            carve_after=carve_after,
            carve_limit=carve_limit,
            min_gap=min_gap,
            start_offset=start_offset,
            end_offset=end_offset,
            deadline=deadline,
            show_progress=True,
        )
    except Exception as e:
        STATE["error"] = f"{type(e).__name__}: {e}"
        STATE["status_line"] = f"failed: {e}"
    finally:
        STATE["running"] = False
        if not STATE["error"]:
            STATE["progress_pct"] = 100.0


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _html(self, data: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._html(_HTML)
        elif self.path == "/api/status":
            self._json(STATE)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or "{}")
        if self.path == "/api/start":
            if STATE["running"]:
                return self._json({"error": "already running"})
            target = (body.get("target") or "").strip()
            if not target:
                return self._json({"error": "empty target"})
            patterns = body.get("patterns") or "*.sig"
            chunk_size = int(body.get("chunk_size") or 1048576)
            results_path = body.get("results_path") or "./results.json"
            carve_before = int(body.get("carve_before") or 4096)
            carve_after = int(body.get("carve_after") or 65536)
            carve_limit = int(body.get("carve_limit") or 100)
            min_gap = int(body.get("min_gap") or 1048576)
            start_offset = int(body.get("start_offset") or 0)
            end_offset = body.get("end_offset")
            if end_offset is not None:
                end_offset = int(end_offset)
            deadline = body.get("deadline")
            if deadline is not None:
                deadline = float(deadline)
            threading.Thread(
                target=_run_scan,
                args=(target, patterns, chunk_size, results_path,
                      carve_before, carve_after, carve_limit, min_gap,
                      start_offset, end_offset, deadline),
                daemon=True,
            ).start()
            self._json({"ok": True, "target": target})
        elif self.path == "/api/stop":
            STATE["error"] = "stopped by user"
            STATE["running"] = False
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, format, *args):  # noqa: A002 — BaseHTTPRequestHandler signature
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    host = "127.0.0.1"
    port = 8787
    server = HTTPServer((host, port), Handler)
    print(f"fixformat v7.0.2 GUI  ->  http://{host}:{port}/", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\nstopped.", flush=True)


if __name__ == "__main__":
    main()
