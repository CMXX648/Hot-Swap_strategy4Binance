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
DEFAULT_CHART_STATE_FILE = "logs/chart_state.json"
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

        if route == "/api/chart":
            chart_file: Path = self.server.chart_state_file  # type: ignore[attr-defined]
            if not chart_file.exists():
                self._json({"ok": False, "error": "chart_state not found — is main.py running?"})
                return
            try:
                state = json.loads(chart_file.read_text(encoding="utf-8"))
                self._json({"ok": True, "state": state})
            except (OSError, json.JSONDecodeError) as e:
                self._json({"ok": False, "error": str(e)})
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
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>SMC Monitor</title>
  <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    :root {
      --bg: #131722; --panel: #1e222d; --border: #2a2e39;
      --text: #d1d4dc; --muted: #787b86;
      --bull: #26a69a; --bear: #ef5350; --accent: #2196f3;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; overflow: hidden; }
    body {
      background: var(--bg); color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      display: flex; flex-direction: column;
    }
    /* ── header ── */
    .hdr {
      display: flex; align-items: center; gap: 10px; flex-shrink: 0;
      padding: 6px 14px; background: var(--panel); border-bottom: 1px solid var(--border);
    }
    .hdr-title { font-size: 13px; font-weight: 700; white-space: nowrap; color: var(--text); }
    .info-bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
    .info-item { font-size: 11px; color: var(--muted); white-space: nowrap; }
    .info-item b { color: var(--text); font-weight: 600; }
    .bull { color: var(--bull) !important; }
    .bear { color: var(--bear) !important; }
    .tabs { display: flex; gap: 4px; margin-left: auto; }
    .tab-btn {
      background: none; border: 1px solid var(--border); color: var(--muted);
      padding: 4px 12px; border-radius: 5px; cursor: pointer;
      font: 12px/1.4 inherit; transition: all .15s;
    }
    .tab-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    .conn { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
    .conn.live { background: var(--bull); box-shadow: 0 0 5px var(--bull); }
    /* ── tabs ── */
    .tab-pane { display: none; flex: 1; flex-direction: column; min-height: 0; overflow: hidden; }
    .tab-pane.active { display: flex; }
    /* ── chart tab ── */
    #chart-wrap { flex: 1; position: relative; min-height: 0; overflow: hidden; }
    #lw-chart { width: 100%; height: 100%; }
    #fvg-overlay { position: absolute; top: 0; left: 0; pointer-events: none; }
    .legend {
      display: flex; gap: 14px; align-items: center; flex-shrink: 0;
      padding: 5px 14px; background: var(--panel); border-top: 1px solid var(--border);
      font-size: 11px; color: var(--muted);
    }
    .lgd { display: flex; align-items: center; gap: 5px; }
    .lgd-sw { width: 14px; height: 9px; border-radius: 2px; }
    /* ── log tab ── */
    .log-toolbar {
      display: flex; gap: 7px; align-items: center; flex-shrink: 0;
      padding: 7px 12px; background: var(--panel); border-bottom: 1px solid var(--border);
    }
    .log-toolbar button, .log-toolbar input {
      border: 1px solid var(--border); background: var(--bg); color: var(--text);
      border-radius: 5px; padding: 4px 10px; font: 12px/1.4 inherit;
    }
    .log-toolbar button.primary { background: var(--accent); color: #fff; border-color: #1565c0; cursor: pointer; }
    #log-status { margin-left: auto; font-size: 11px; color: var(--muted); }
    #log-panel {
      flex: 1; overflow: auto; padding: 8px 12px; white-space: pre-wrap;
      line-height: 1.45; font-size: 12px; background: var(--bg);
    }
    .line.err { color: #ef5350; }
    .line.warn { color: #ffb300; }
    .line.ok { color: #26a69a; }
  </style>
</head>
<body>
  <!-- header -->
  <div class="hdr">
    <span class="hdr-title">⚡ SMC Monitor</span>
    <div class="info-bar">
      <div class="info-item">对: <b id="i-sym">—</b></div>
      <div class="info-item">周期: <b id="i-itv">—</b></div>
      <div class="info-item">趋势: <b id="i-trend">—</b></div>
      <div class="info-item">ATR: <b id="i-atr">—</b></div>
      <div class="info-item">更新: <b id="i-upd">—</b></div>
      <div class="info-item">FVG: <b id="i-fvg">—</b> &nbsp;OB: <b id="i-ob">—</b></div>
    </div>
    <div class="tabs">
      <button class="tab-btn active" data-tab="chart">📊 K线</button>
      <button class="tab-btn" data-tab="logs">📋 日志</button>
    </div>
    <div class="conn" id="conn-dot"></div>
  </div>

  <!-- chart tab -->
  <div class="tab-pane active" id="tab-chart">
    <div id="chart-wrap">
      <div id="lw-chart"></div>
      <canvas id="fvg-overlay"></canvas>
    </div>
    <div class="legend">
      <div class="lgd"><div class="lgd-sw" style="background:rgba(38,166,154,.3);border:1px solid #26a69a"></div>多头FVG</div>
      <div class="lgd"><div class="lgd-sw" style="background:rgba(239,83,80,.3);border:1px solid #ef5350"></div>空头FVG</div>
      <div class="lgd"><div class="lgd-sw" style="background:rgba(33,150,243,.25);border:1px solid #2196f3"></div>多头OB</div>
      <div class="lgd"><div class="lgd-sw" style="background:rgba(255,152,0,.25);border:1px solid #ff9800"></div>空头OB</div>
      <span style="margin-left:auto;font-size:10px;color:var(--muted)" id="legend-note">等待数据…</span>
    </div>
  </div>

  <!-- log tab -->
  <div class="tab-pane" id="tab-logs">
    <div class="log-toolbar">
      <button data-lines="100">100行</button>
      <button data-lines="300">300行</button>
      <button data-lines="1000">1000行</button>
      <input id="token" placeholder="token（可选）" style="width:120px" />
      <button class="primary" id="reload">刷新</button>
      <label style="font-size:12px;color:var(--muted)">
        <input type="checkbox" id="auto-stream" checked /> 实时流
      </label>
      <div id="log-status">就绪</div>
    </div>
    <div id="log-panel"></div>
  </div>

  <script>
  (function () {
    'use strict';

    /* ======== Tab switching ======== */
    const tabBtns = document.querySelectorAll('.tab-btn');
    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        tabBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        if (btn.dataset.tab === 'chart') { resizeChart(); }
        if (btn.dataset.tab === 'logs' && !logInited) { logInited = true; loadLogs(); startStream(); }
      });
    });

    /* ======== Lightweight Charts ======== */
    const chartWrap = document.getElementById('chart-wrap');
    const overlay   = document.getElementById('fvg-overlay');

    const chart = LightweightCharts.createChart(document.getElementById('lw-chart'), {
      width:  chartWrap.clientWidth  || 800,
      height: chartWrap.clientHeight || 500,
      layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
      grid:   { vertLines: { color: '#2a2e39' }, horzLines: { color: '#2a2e39' } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#2a2e39', scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false },
    });

    const cSeries = chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    });

    function resizeChart() {
      const w = chartWrap.clientWidth, h = chartWrap.clientHeight;
      if (w > 0 && h > 0) { chart.resize(w, h); overlay.width = w; overlay.height = h; }
    }
    new ResizeObserver(resizeChart).observe(chartWrap);
    resizeChart();

    /* ======== Overlay drawing (rAF loop) ======== */
    let chartData = null;

    function drawOverlay() {
      const ctx = overlay.getContext('2d');
      ctx.clearRect(0, 0, overlay.width, overlay.height);
      if (!chartData) return;

      const ts = chart.timeScale();

      function box(top, bot, lt, fill, stroke, label) {
        const x1 = ts.timeToCoordinate(lt);
        if (x1 === null) return;
        const y1 = cSeries.priceToCoordinate(top);
        const y2 = cSeries.priceToCoordinate(bot);
        if (y1 === null || y2 === null) return;
        const lx = Math.max(0, x1);
        const ty = Math.min(y1, y2);
        const bw = overlay.width - lx;
        const bh = Math.max(1, Math.abs(y2 - y1));
        ctx.fillStyle = fill;   ctx.fillRect(lx, ty, bw, bh);
        ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.strokeRect(lx, ty, bw, bh);
        if (label && bh > 14) {
          ctx.fillStyle = stroke; ctx.font = '10px monospace';
          ctx.fillText(label, lx + 4, ty + 12);
        }
      }

      for (const f of (chartData.fvgs || [])) {
        const b = f.bias === 'BULLISH';
        box(f.top, f.bottom, f.left_time,
            b ? 'rgba(38,166,154,.15)'  : 'rgba(239,83,80,.15)',
            b ? 'rgba(38,166,154,.75)'  : 'rgba(239,83,80,.75)',
            b ? 'FVG ▲' : 'FVG ▼');
      }
      for (const o of (chartData.order_blocks || [])) {
        const b = o.bias === 'BULLISH';
        box(o.high, o.low, o.time,
            b ? 'rgba(33,150,243,.15)'  : 'rgba(255,152,0,.15)',
            b ? 'rgba(33,150,243,.6)'   : 'rgba(255,152,0,.6)',
            b ? 'OB ▲' : 'OB ▼');
      }
    }

    (function rafLoop() { drawOverlay(); requestAnimationFrame(rafLoop); })();

    /* ======== Chart data update ======== */
    function applyState(state) {
      /* candles */
      const bars = (state.candles || []).map(c => ({
        time: c.t, open: c.o, high: c.h, low: c.l, close: c.c,
      }));
      if (state.current_candle) {
        const cc = state.current_candle;
        const live = { time: cc.t, open: cc.o, high: cc.h, low: cc.l, close: cc.c };
        if (bars.length && bars[bars.length - 1].time === live.time) {
          bars[bars.length - 1] = live;
        } else {
          bars.push(live);
        }
      }
      cSeries.setData(bars);

      /* BOS / CHoCH markers */
      const markers = (state.structure_events || []).map(e => ({
        time:     e.bar_time,
        position: e.bias === 'BULLISH' ? 'belowBar' : 'aboveBar',
        color:    e.bias === 'BULLISH' ? '#26a69a' : '#ef5350',
        shape:    e.bias === 'BULLISH' ? 'arrowUp' : 'arrowDown',
        text:     e.tag + (e.bias === 'BULLISH' ? ' ▲' : ' ▼'),
      }));
      cSeries.setMarkers(markers);

      /* info bar */
      document.getElementById('i-sym').textContent  = state.symbol   || '—';
      document.getElementById('i-itv').textContent  = state.interval || '—';
      const tEl = document.getElementById('i-trend');
      if (state.swing_trend === 'BULLISH') {
        tEl.textContent = '多 ▲'; tEl.className = 'bull';
      } else if (state.swing_trend === 'BEARISH') {
        tEl.textContent = '空 ▼'; tEl.className = 'bear';
      } else {
        tEl.textContent = '中性'; tEl.className = '';
      }
      document.getElementById('i-atr').textContent = state.atr ? state.atr.toFixed(2) : '—';
      const t = state.updated_at ? new Date(state.updated_at) : null;
      document.getElementById('i-upd').textContent = t ? t.toLocaleTimeString('zh-CN') : '—';
      document.getElementById('i-fvg').textContent  = (state.fvgs          || []).length;
      document.getElementById('i-ob').textContent   = (state.order_blocks  || []).length;
      document.getElementById('legend-note').textContent =
        'K线: ' + (state.candles || []).length + '根  |  ' + (state.symbol || '') + ' ' + (state.interval || '');
    }

    /* ======== Polling /api/chart every 2 s ======== */
    let prevTs = 0;
    const connDot = document.getElementById('conn-dot');

    async function pollChart() {
      try {
        const res = await fetch('/api/chart');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.ok && data.state && data.state.updated_at !== prevTs) {
          prevTs = data.state.updated_at;
          chartData = data.state;
          applyState(chartData);
        }
        connDot.classList.add('live');
      } catch (_) {
        connDot.classList.remove('live');
      }
    }
    pollChart();
    setInterval(pollChart, 2000);

    /* ======== Log viewer ======== */
    let logInited = false;
    const logEl    = document.getElementById('log-panel');
    const statusEl = document.getElementById('log-status');
    const tokenEl  = document.getElementById('token');
    const autoEl   = document.getElementById('auto-stream');
    let currentLines = 300, es = null;

    function lineClass(l) {
      if (/ERROR|异常|失败|Traceback/i.test(l)) return 'err';
      if (/WARN|warning|超时|重连|FILTER/i.test(l)) return 'warn';
      if (/交易信号|持仓建立|止盈|止损|OK|GAP FILL/i.test(l)) return 'ok';
      return '';
    }
    function appendLine(text) {
      const d = document.createElement('div');
      d.className = 'line ' + lineClass(text); d.textContent = text;
      logEl.appendChild(d);
    }
    async function loadLogs() {
      const qp = new URLSearchParams({ lines: String(currentLines) });
      const tok = tokenEl.value.trim(); if (tok) qp.set('token', tok);
      statusEl.textContent = '加载中…';
      try {
        const res = await fetch('/api/logs?' + qp);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        logEl.innerHTML = '';
        (data.lines || []).forEach(appendLine);
        logEl.scrollTop = logEl.scrollHeight;
        statusEl.textContent = '已加载 ' + (data.count || 0) + ' 行';
      } catch (e) { statusEl.textContent = '失败: ' + e.message; }
    }
    function startStream() {
      if (es) es.close();
      if (!autoEl.checked) return;
      const qp = new URLSearchParams();
      const tok = tokenEl.value.trim(); if (tok) qp.set('token', tok);
      es = new EventSource('/api/stream?' + qp);
      es.onmessage = evt => {
        try { appendLine(JSON.parse(evt.data).line || ''); logEl.scrollTop = logEl.scrollHeight; statusEl.textContent = '实时'; }
        catch (_) {}
      };
      es.onerror = () => { statusEl.textContent = '流断开，自动重连…'; };
    }
    document.querySelectorAll('button[data-lines]').forEach(btn =>
      btn.addEventListener('click', async () => { currentLines = +btn.dataset.lines || 300; await loadLogs(); }));
    document.getElementById('reload').addEventListener('click', async () => { await loadLogs(); startStream(); });
    autoEl.addEventListener('change', startStream);

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
    parser.add_argument("--chart-state", default=DEFAULT_CHART_STATE_FILE, help="Path to chart_state.json written by main.py")
    parser.add_argument("--token", default=os.environ.get("LOG_WEB_TOKEN", ""), help="Optional access token")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), LogHandler)
    server.log_file = Path(args.log_file).resolve()
    server.chart_state_file = Path(args.chart_state).resolve()
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
