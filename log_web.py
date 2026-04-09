#!/usr/bin/env python3
"""
SMC Log Web Viewer
- /            : log dashboard page
- /api/logs    : recent log lines (JSON)
- /api/stream  : live log stream (SSE)

Designed for private access over Tailscale.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_LOG_FILE = "logs/smctrade-service.log"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_LINES = 2000


def _tail_lines(file_path: Path, lines: int) -> list[str]:
    """Return the last N lines from file as UTF-8 text."""
    if lines <= 0:
        return []

    lines = min(lines, MAX_LINES)
    if not file_path.exists():
        return []

    out = deque(maxlen=lines)
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                out.append(line.rstrip("\n"))
    except OSError:
        return []

    return list(out)


def _check_token(handler: BaseHTTPRequestHandler, token: Optional[str]) -> bool:
    """Validate optional token via query or header."""
    if not token:
        return True

    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    query_token = (qs.get("token") or [""])[0]
    header_token = handler.headers.get("X-Log-Token", "")
    return query_token == token or header_token == token


class LogHandler(BaseHTTPRequestHandler):
    server_version = "SMCLogWeb/1.0"

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _unauthorized(self) -> None:
        self._json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)

    def do_GET(self) -> None:  # noqa: N802
        log_file: Path = self.server.log_file  # type: ignore[attr-defined]
        token: Optional[str] = self.server.token  # type: ignore[attr-defined]

        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._html(_INDEX_HTML)
            return

        if route == "/api/health":
            self._json({"ok": True, "log_file": str(log_file), "exists": log_file.exists()})
            return

        if route == "/api/logs":
            if not _check_token(self, token):
                self._unauthorized()
                return

            qs = parse_qs(parsed.query)
            try:
                lines = int((qs.get("lines") or ["200"])[0])
            except ValueError:
                lines = 200

            data = _tail_lines(log_file, lines)
            self._json({"ok": True, "lines": data, "count": len(data)})
            return

        if route == "/api/stream":
            if not _check_token(self, token):
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.end_headers()
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                pos = 0
                if log_file.exists():
                    pos = log_file.stat().st_size

                while True:
                    # Keep-alive ping for proxies/clients.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()

                    if log_file.exists():
                        current_size = log_file.stat().st_size
                        if current_size < pos:
                            # Log rotated/truncated.
                            pos = 0

                        if current_size > pos:
                            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                                f.seek(pos)
                                chunk = f.read()
                                pos = f.tell()

                            for line in chunk.splitlines():
                                event = json.dumps({"line": line}, ensure_ascii=False)
                                self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
                            self.wfile.flush()

                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Keep server output clean.
        return


_INDEX_HTML = """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>SMC Logs</title>
  <style>
    :root {
      --bg: #f4f1ea;
      --panel: #fffaf0;
      --ink: #1f1b16;
      --muted: #6b5f50;
      --accent: #be5a2a;
      --ok: #0f766e;
      --err: #b91c1c;
      --warn: #b45309;
    }
    body {
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background: radial-gradient(circle at top right, #fbe7c6 0%, var(--bg) 45%, #efe6d8 100%);
      color: var(--ink);
    }
    .wrap {
      max-width: 1100px;
      margin: 20px auto;
      padding: 0 14px;
    }
    .card {
      background: var(--panel);
      border: 1px solid #e5d9c8;
      border-radius: 14px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.06);
      overflow: hidden;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      padding: 12px;
      border-bottom: 1px solid #eadfce;
      background: linear-gradient(90deg, #fff9ef 0%, #fff3df 100%);
    }
    button, input {
      border: 1px solid #d8c8b2;
      background: #fff;
      color: var(--ink);
      border-radius: 8px;
      padding: 8px 10px;
      font: inherit;
      font-size: 13px;
    }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: #9e4a22;
      cursor: pointer;
    }
    .status {
      margin-left: auto;
      font-size: 12px;
      color: var(--muted);
    }
    #log {
      height: 72vh;
      overflow: auto;
      margin: 0;
      padding: 12px;
      white-space: pre-wrap;
      line-height: 1.45;
      font-size: 12px;
      background: #fffdf8;
    }
    .line.err { color: var(--err); }
    .line.warn { color: var(--warn); }
    .line.ok { color: var(--ok); }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <div class=\"toolbar\">
        <button data-lines=\"100\">最近100行</button>
        <button data-lines=\"300\">最近300行</button>
        <button data-lines=\"1000\">最近1000行</button>
        <input id=\"token\" placeholder=\"可选口令 token\" />
        <button class=\"primary\" id=\"reload\">刷新</button>
        <label><input type=\"checkbox\" id=\"auto\" checked /> 实时流</label>
        <div class=\"status\" id=\"status\">准备就绪</div>
      </div>
      <div id=\"log\"></div>
    </div>
  </div>

  <script>
    const logEl = document.getElementById('log');
    const statusEl = document.getElementById('status');
    const tokenEl = document.getElementById('token');
    const autoEl = document.getElementById('auto');
    const reloadBtn = document.getElementById('reload');
    let currentLines = 300;
    let es = null;

    function lineClass(line) {
      if (/ERROR|异常|失败|Traceback/i.test(line)) return 'err';
      if (/WARN|warning|超时|重连|FILTER/i.test(line)) return 'warn';
      if (/交易信号|持仓建立|止盈|止损|OK|GAP FILL/i.test(line)) return 'ok';
      return '';
    }

    function appendLine(text) {
      const div = document.createElement('div');
      div.className = `line ${lineClass(text)}`;
      div.textContent = text;
      logEl.appendChild(div);
    }

    function setStatus(text) {
      statusEl.textContent = text;
    }

    async function loadLogs() {
      const token = tokenEl.value.trim();
      const qp = new URLSearchParams({ lines: String(currentLines) });
      if (token) qp.set('token', token);

      setStatus('加载中...');
      const res = await fetch(`/api/logs?${qp.toString()}`);
      if (!res.ok) {
        setStatus(`加载失败: HTTP ${res.status}`);
        return;
      }

      const data = await res.json();
      logEl.innerHTML = '';
      for (const line of data.lines || []) appendLine(line);
      logEl.scrollTop = logEl.scrollHeight;
      setStatus(`已加载 ${data.count || 0} 行`);
    }

    function startStream() {
      if (es) es.close();
      if (!autoEl.checked) return;

      const token = tokenEl.value.trim();
      const qp = new URLSearchParams();
      if (token) qp.set('token', token);
      es = new EventSource(`/api/stream?${qp.toString()}`);

      es.onmessage = (evt) => {
        try {
          const obj = JSON.parse(evt.data);
          appendLine(obj.line || '');
          logEl.scrollTop = logEl.scrollHeight;
          setStatus('实时连接中');
        } catch (_) {}
      };

      es.onerror = () => setStatus('实时流断开，浏览器会自动重连');
    }

    document.querySelectorAll('button[data-lines]').forEach(btn => {
      btn.addEventListener('click', async () => {
        currentLines = Number(btn.dataset.lines || '300');
        await loadLogs();
      });
    });

    reloadBtn.addEventListener('click', async () => {
      await loadLogs();
      startStream();
    });

    autoEl.addEventListener('change', () => startStream());

    (async function init() {
      await loadLogs();
      startStream();
    })();
  </script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC log web viewer")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Listen port (default: 8080)")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="Path to log file")
    parser.add_argument("--token", default=os.environ.get("LOG_WEB_TOKEN", ""), help="Optional access token")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), LogHandler)
    server.log_file = Path(args.log_file).resolve()
    server.token = args.token if args.token else None

    print(f"[LogWeb] Listening on http://{args.host}:{args.port}")
    print(f"[LogWeb] Log file: {server.log_file}")
    if server.token:
        print("[LogWeb] Token auth: enabled")
    else:
        print("[LogWeb] Token auth: disabled (use only in trusted network)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
