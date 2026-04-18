from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

BASE = Path('/opt/agent-cp')
PORT = int(os.getenv('PORT', '8092'))
VPS_MCP_URL = os.getenv('VPS_MCP_URL', 'http://localhost:8093')
MCP_HEALTH_URL = f'{VPS_MCP_URL}/health'


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        data[key.strip()] = value.strip()
    return data


ENV = load_env_file(BASE / '.env')
CP_USER = os.getenv('CP_USER', ENV.get('CP_USER', 'admin'))
CP_PASS = os.getenv('CP_PASS', ENV.get('CP_PASS', ''))
VPS_MCP_TOKEN = os.getenv('VPS_MCP_TOKEN', ENV.get('VPS_MCP_TOKEN', ''))

app = FastAPI(docs_url=None, redoc_url=None)
basic = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(basic)) -> str:
    if not (creds.username == CP_USER and creds.password == CP_PASS):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={'WWW-Authenticate': 'Basic realm=agent-cp'})
    return creds.username


def _vps_request(method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> Any:
    url = f'{VPS_MCP_URL}{path}'
    data = json.dumps(body).encode('utf-8') if body is not None else None
    headers: dict[str, str] = {'Accept': 'application/json'}
    if data:
        headers['Content-Type'] = 'application/json'
    if VPS_MCP_TOKEN:
        headers['Authorization'] = f'Bearer {VPS_MCP_TOKEN}'
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def query_logs(limit: int = 100, source: str = 'all', q: str = '', errors_only: bool = False) -> list[dict[str, Any]]:
    params = urlencode({'limit': limit, 'source': source, 'q': q, 'errors_only': '1' if errors_only else '0'})
    try:
        result = _vps_request('GET', f'/logs?{params}')
        return result if isinstance(result, list) else []
    except Exception:
        return []


def query_stats() -> dict[str, Any]:
    try:
        result = _vps_request('GET', '/stats')
        return result if isinstance(result, dict) else {}
    except Exception:
        return {'last_50': [], 'total_errors': 0, 'top_commands': []}


def query_log_by_id(entry_id: int) -> dict[str, Any]:
    try:
        return _vps_request('GET', f'/logs/{entry_id}')
    except HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(status_code=404, detail='log not found')
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def mcp_health() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        req = Request(MCP_HEALTH_URL, headers={'Accept': 'application/json'})
        with urlopen(req, timeout=2.5) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        state = str(payload.get('state') or ('executing' if payload.get('current_command') else 'idle')).lower()
        return {
            'reachable': True,
            'state': state,
            'updated_at': payload.get('updated_at'),
            'current_command': payload.get('current_command'),
            'rate_1m': payload.get('rate_1m', 0),
            'base_dir': payload.get('base_dir'),
            'mcp_ready': payload.get('mcp_ready', True),
            'sse': payload.get('sse'),
            'messages': payload.get('messages'),
            'checked_at': now,
            'health_url': MCP_HEALTH_URL,
        }
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
        return {
            'reachable': False,
            'state': 'down',
            'error': str(exc),
            'checked_at': now,
            'health_url': MCP_HEALTH_URL,
        }


HTML = r"""<!doctype html>
<html lang="hu">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Agent Control Panel</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #09111f;
      --panel: rgba(12, 18, 32, 0.92);
      --card: rgba(16, 23, 39, 0.96);
      --border: rgba(148, 163, 184, 0.16);
      --text: #e8eef8;
      --muted: #91a4bf;
      --accent: #6e8cff;
      --ok: #22c55e;
      --warn: #f59e0b;
      --down: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; width: 100%; max-width: 100vw; overflow-x: hidden;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(110,140,255,0.18), transparent 30%),
        radial-gradient(circle at top right, rgba(34,197,94,0.10), transparent 24%),
        var(--bg);
    }
    .page { max-width: 1400px; width: min(100%, 1400px); margin: 0 auto; padding: 18px 14px 32px; overflow-x: hidden; }
    .header, .card {
      min-width: 0; max-width: 100%;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 18px; box-shadow: 0 18px 42px rgba(0,0,0,0.24);
    }
    .header {
      display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
      padding: 16px 18px;
    }
    .brand { display: grid; gap: 4px; }
    h1 { margin: 0; font-size: 24px; letter-spacing: -0.02em; }
    .sub { color: var(--muted); font-size: 13px; }

    /* ── MCP State orb ── */
    .status-hero {
      display: flex; align-items: center; gap: 14px; padding: 12px 14px; border-radius: 16px;
      background: rgba(8,13,25,0.76); border: 1px solid var(--border); max-width: 100%; min-width: 0;
    }
    .status-visual {
      --acc: #b66cff; --acc-soft: rgba(182,108,255,0.32); --acc-hot: rgba(231,203,255,0.95);
      --glow: rgba(182,108,255,0.34); --wspd: 4.8s;
      width: 82px; height: 82px; flex: 0 0 auto; position: relative;
      display: grid; place-items: center; border-radius: 50%;
      background:
        radial-gradient(circle at 50% 50%, rgba(255,255,255,0.16), rgba(255,255,255,0.0) 28%),
        radial-gradient(circle at 50% 50%, rgba(182,108,255,0.20), rgba(10,15,28,0.0) 66%);
      box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 28px var(--glow), 0 0 58px rgba(121,69,255,0.16);
      overflow: hidden; color: var(--acc); isolation: isolate;
      transition: filter 0.6s ease, box-shadow 0.6s ease;
    }
    .status-visual::before, .status-visual::after {
      content: ''; position: absolute; inset: 8px; border-radius: 50%; pointer-events: none;
    }
    .status-visual::before {
      background:
        radial-gradient(circle at 50% 42%, rgba(255,255,255,0.26), rgba(255,255,255,0.03) 18%, rgba(255,255,255,0.0) 42%),
        radial-gradient(circle at 50% 58%, rgba(255,255,255,0.14), rgba(255,255,255,0.0) 34%);
      filter: blur(1px); opacity: .9; mix-blend-mode: screen;
    }
    .status-visual::after {
      inset: -10px; border: 1px solid rgba(255,255,255,0.08);
      box-shadow: inset 0 0 30px rgba(255,255,255,0.05); opacity: .35; filter: blur(.4px);
    }
    .status-svg { width: 100%; height: 100%; display: block; }
    .status-wave {
      fill: none; stroke: currentColor; stroke-linecap: round; opacity: .72;
      transform-origin: 50% 50%; filter: drop-shadow(0 0 8px currentColor);
    }
    .status-wave.wave-1 { stroke-width: 1.5; stroke-dasharray: 18 9; animation: waveSpin var(--wspd) linear infinite; }
    .status-wave.wave-2 { stroke-width: 1.2; stroke-dasharray: 9 12; opacity: .42; animation: waveSpin calc(var(--wspd)*1.36) linear infinite reverse; }
    .status-wave.wave-3 { stroke-width: 1.0; stroke-dasharray: 4 15; opacity: .22; animation: waveSpin calc(var(--wspd)*1.8) linear infinite; }
    .status-halo { fill: url(#halo-gradient); opacity: .95; mix-blend-mode: screen; animation: haloPulse 4.6s ease-in-out infinite; }
    .status-core { fill: url(#core-gradient); transform-origin: 50% 50%; animation: corePulse 3.8s ease-in-out infinite; filter: url(#status-blur); }
    .status-spark { fill: rgba(255,255,255,0.95); opacity: .82; filter: drop-shadow(0 0 8px rgba(255,255,255,0.95)); animation: sparkPulse 2.8s ease-in-out infinite; }
    .status-visual[data-state='idle'] {
      --acc: #bd77ff; --acc-soft: rgba(189,119,255,0.32); --acc-hot: rgba(247,235,255,0.92);
      --glow: rgba(168,85,247,0.42); --wspd: 6.4s;
    }
    .status-visual[data-state='executing'] {
      --acc: #ffb357; --acc-soft: rgba(255,179,87,0.30); --acc-hot: rgba(255,250,241,1);
      --glow: rgba(255,170,64,0.52); --wspd: 1.6s;
      filter: saturate(1.3) brightness(1.1);
    }
    .status-visual[data-state='down'] {
      --acc: #ff5f74; --acc-soft: rgba(255,95,116,0.24); --acc-hot: rgba(255,224,228,0.88);
      --glow: rgba(255,95,116,0.42); --wspd: 3.8s;
      filter: saturate(1.22) brightness(0.95);
      animation: statusFlicker 2.4s steps(1,end) infinite;
    }
    .status-text { display: grid; gap: 2px; min-width: 0; }
    .status-title { font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
    .status-line { font-size: 15px; font-weight: 700; }
    .status-meta { color: var(--muted); font-size: 12px; }
    @keyframes waveSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    @keyframes haloPulse { 0%,100% { opacity:.72; transform:scale(0.98); } 50% { opacity:1; transform:scale(1.02); } }
    @keyframes corePulse { 0%,100% { transform:scale(0.94); opacity:.84; } 50% { transform:scale(1.04); opacity:1; } }
    @keyframes sparkPulse { 0%,100% { opacity:.62; transform:translateY(-1px) scale(0.94); } 50% { opacity:1; transform:translateY(0) scale(1.08); } }
    @keyframes statusFlicker {
      0%,100% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 28px rgba(255,95,116,0.42), 0 0 58px rgba(255,95,116,0.15); }
      50% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 20px rgba(255,95,116,0.24), 0 0 38px rgba(255,95,116,0.08); }
      52% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 34px rgba(255,95,116,0.52), 0 0 68px rgba(255,95,116,0.18); }
      70% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 24px rgba(255,95,116,0.32), 0 0 50px rgba(255,95,116,0.12); }
    }

    /* ── Layout ── */
    .grid.two { grid-template-columns: 1fr; }
    .card { padding: 16px; overflow-x: hidden; }
    .section-title { margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; font-size: 12px; }
    .monitor { display: grid; gap: 10px; }
    .monitor-row {
      display: grid; grid-template-columns: minmax(0,1.3fr) minmax(0,1fr) auto auto; gap: 10px;
      align-items: center; padding: 12px 0; border-bottom: 1px solid rgba(148,163,184,0.12); min-width: 0;
    }
    .monitor-row > * { min-width: 0; }
    .monitor-row:last-child { border-bottom: none; }
    .monitor-row.no-actions { grid-template-columns: minmax(0,1.3fr) minmax(0,1fr) auto; }
    .monitor-name { font-weight: 700; }
    .monitor-url, .monitor-note, .mono, input, textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .monitor-url, .monitor-note { color: var(--muted); font-size: 12px; word-break: break-word; overflow-wrap: anywhere; }
    .pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 11px; border-radius: 999px; border: 1px solid var(--border); background: rgba(8,13,25,0.62); font-size: 12px; font-weight: 700; white-space: nowrap; }
    .pill .dot { width: 8px; height: 8px; border-radius: 999px; background: currentColor; }
    .pill.idle { color: var(--ok); }
    .pill.executing { color: var(--warn); }
    .pill.down { color: var(--down); }
    .pill.unknown { color: #94a3b8; }

    /* ── Kill / Start switch ── */
    .svc-btns { display: flex; gap: 6px; flex-wrap: nowrap; }
    .svc-btn {
      padding: 6px 13px; border-radius: 999px; border: 1px solid var(--border);
      background: rgba(8,13,25,0.82); color: var(--text); cursor: pointer;
      font-size: 12px; font-weight: 700; transition: opacity 0.2s, border-color 0.2s;
      white-space: nowrap;
    }
    .svc-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .svc-btn.kill { border-color: rgba(239,68,68,0.5); color: var(--down); }
    .svc-btn.kill:hover:not(:disabled) { border-color: var(--down); background: rgba(239,68,68,0.12); }
    .svc-btn.start { border-color: rgba(34,197,94,0.5); color: var(--ok); }
    .svc-btn.start:hover:not(:disabled) { border-color: var(--ok); background: rgba(34,197,94,0.12); }

    .content { display: grid; gap: 14px; grid-template-columns: 1fr; min-width: 0; max-width: 100%; }
    .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; min-width: 0; max-width: 100%; }
    .toolbar > * { flex: 1 1 auto; min-width: 0; max-width: 100%; }
    input, textarea, button {
      border: 1px solid var(--border); border-radius: 14px; background: rgba(8,13,25,0.88); color: var(--text);
      padding: 12px 14px; outline: none; min-width: 0; max-width: 100%;
    }
    textarea { width: 100%; min-height: 180px; resize: vertical; }
    input:focus, textarea:focus { border-color: rgba(110,140,255,0.75); box-shadow: 0 0 0 3px rgba(110,140,255,0.10); }
    button { cursor: pointer; font-weight: 700; }
    button:hover { border-color: rgba(110,140,255,0.55); }
    .exec-row { display: grid; gap: 10px; grid-template-columns: 1fr 140px; margin-top: 10px; }
    .muted { color: var(--muted); font-size: 13px; }
    .stats { display: grid; gap: 10px; grid-template-columns: repeat(3, minmax(0,1fr)); }
    .stat { padding: 14px; border-radius: 14px; background: rgba(8,13,25,0.68); border: 1px solid var(--border); }
    .stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 6px; }
    .stat .value { font-size: 28px; font-weight: 800; transition: opacity 0.2s; }
    .table-wrap { overflow-x: auto; max-width: 100%; -webkit-overflow-scrolling: touch; }
    table { width: 100%; max-width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { text-align: left; border-bottom: 1px solid rgba(148,163,184,0.12); padding: 10px 8px; vertical-align: top; word-break: break-word; overflow-wrap: anywhere; min-width: 0; }
    th { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
    .logs { display: grid; gap: 12px; }
    .log-card { padding: 14px; border-radius: 16px; background: linear-gradient(180deg, rgba(16,23,39,0.98), rgba(12,18,32,0.98)); border: 1px solid var(--border); min-width: 0; overflow-x: hidden; }
    .log-card.log-new { animation: logSlideIn 0.3s ease; }
    @keyframes logSlideIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }
    .log-head { display: grid; gap: 8px; grid-template-columns: minmax(0,180px) minmax(0,auto) minmax(0,1fr) auto; align-items: center; }
    .log-head > * { min-width: 0; }
    .log-cmd, pre { white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; max-width: 100%; }
    .log-cmd { margin-top: 10px; font-size: 13px; line-height: 1.45; min-width: 0; }
    .log-summary { min-width: 0; word-break: break-word; overflow-wrap: anywhere; }
    pre { margin: 0; padding: 12px; border-radius: 12px; background: rgba(8,13,25,0.82); border: 1px solid rgba(148,163,184,0.12); max-height: 160px; overflow: auto; font-size: 12px; }
    .result { margin-top: 12px; }
    .tabbar { display: flex; gap: 10px; flex-wrap: wrap; }
    .tab-btn { padding: 10px 14px; border-radius: 999px; border: 1px solid var(--border); background: rgba(8,13,25,0.82); }
    .tab-btn.active { border-color: rgba(110,140,255,0.7); background: rgba(110,140,255,0.14); }
    .view { display: none; }
    .view.active { display: block; }
    @media (min-width: 980px) {
      .grid.two { grid-template-columns: 1.05fr .95fr; align-items: start; }
      .content { grid-template-columns: 1.05fr .95fr; align-items: start; }
    }
    @media (max-width: 780px) {
      .header { padding: 14px; }
      .stats { grid-template-columns: 1fr; }
      .log-head, .monitor-row, .exec-row { grid-template-columns: 1fr; }
      .status-hero { width: 100%; }
      .page { padding-left: 12px; padding-right: 12px; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="brand">
        <h1>Agent Control Panel</h1>
        <div class="sub">port __PORT__ &middot; vps-mcp monitoring dashboard</div>
      </div>
      <div class="status-hero">
        <div class="status-visual" id="mcp-visual" data-state="unknown" aria-hidden="true">
          <svg class="status-svg" viewBox="0 0 120 120">
            <defs>
              <filter id="status-blur" x="-40%" y="-40%" width="180%" height="180%">
                <feGaussianBlur stdDeviation="1.8" />
              </filter>
              <radialGradient id="halo-gradient" cx="50%" cy="42%" r="62%">
                <stop offset="0%" stop-color="var(--acc-hot)" stop-opacity="0.95" />
                <stop offset="34%" stop-color="var(--acc)" stop-opacity="0.34" />
                <stop offset="70%" stop-color="var(--acc-soft)" stop-opacity="0.14" />
                <stop offset="100%" stop-color="rgba(0,0,0,0)" stop-opacity="0" />
              </radialGradient>
              <radialGradient id="core-gradient" cx="50%" cy="42%" r="62%">
                <stop offset="0%" stop-color="var(--acc-hot)" stop-opacity="1" />
                <stop offset="18%" stop-color="var(--acc)" stop-opacity="0.96" />
                <stop offset="48%" stop-color="var(--acc-soft)" stop-opacity="0.72" />
                <stop offset="100%" stop-color="rgba(0,0,0,0)" stop-opacity="0" />
              </radialGradient>
            </defs>
            <circle class="status-wave wave-3" cx="60" cy="60" r="45" />
            <circle class="status-wave wave-2" cx="60" cy="60" r="36" />
            <circle class="status-wave wave-1" cx="60" cy="60" r="28" />
            <circle class="status-halo" cx="60" cy="60" r="42" />
            <circle class="status-core" cx="60" cy="60" r="22" />
            <circle class="status-spark" cx="54" cy="47" r="4.2" />
          </svg>
        </div>
        <div class="status-text">
          <div class="status-title">vps-mcp state</div>
          <div class="status-line" id="mcp-state">checking&hellip;</div>
          <div class="status-meta" id="mcp-meta">polling __VPS_MCP_URL__/health</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-title">Monitoring</div>
      <div class="monitor" id="monitor-list">
        <div class="monitor-row no-actions">
          <div>
            <div class="monitor-name">Dashboard API</div>
            <div class="monitor-url">/exec, /stats, /logs, /mcp-health</div>
          </div>
          <div class="monitor-note">Local control surface</div>
          <div class="pill idle"><span class="dot"></span>ready</div>
        </div>
        <div class="monitor-row">
          <div>
            <div class="monitor-name">vps-mcp SSE server</div>
            <div class="monitor-url">__VPS_MCP_URL__/health</div>
          </div>
          <div class="monitor-note" id="monitor-note">polling&hellip;</div>
          <div class="pill unknown" id="monitor-pill"><span class="dot"></span>unknown</div>
          <div class="svc-btns">
            <button class="svc-btn kill" id="btn-kill" title="Stop vps-mcp service">Kill</button>
            <button class="svc-btn start" id="btn-start" title="Start vps-mcp service">Start</button>
          </div>
        </div>
      </div>
    </div>

    <div class="tabbar">
      <button class="tab-btn active" data-tab="exec" type="button">Exec</button>
      <button class="tab-btn" data-tab="logs" type="button">Logs</button>
    </div>

    <div class="view active" id="exec-view">
      <div class="content">
        <div class="card">
          <div class="section-title">Run command (via vps-mcp)</div>
          <form id="exec-form">
            <textarea id="cmd" name="cmd" placeholder="Enter a shell command"></textarea>
            <div class="exec-row">
              <input id="source-input" name="source" value="user" placeholder="source" />
              <button type="submit">Run</button>
            </div>
          </form>
          <div id="last-result" class="result" style="display:none">
            <div class="muted">exit code <span id="result-code"></span></div>
            <div class="muted" id="exec-status" style="margin-top:4px"></div>
            <div style="margin-top:10px">
              <div class="section-title" style="margin-bottom:8px">stdout</div>
              <pre id="result-stdout"></pre>
            </div>
            <div style="margin-top:10px">
              <div class="section-title" style="margin-bottom:8px">stderr</div>
              <pre id="result-stderr"></pre>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="section-title">Stats</div>
          <div class="stats">
            <div class="stat"><div class="label">Total errors</div><div class="value" id="total-errors">—</div></div>
            <div class="stat"><div class="label">Last 50 commands</div><div class="value" id="last50-count">—</div></div>
            <div class="stat"><div class="label">Top commands</div><div class="value" id="top10-count">—</div></div>
          </div>
          <div style="margin-top:14px" class="table-wrap">
            <table><thead><tr><th>Command</th><th>Count</th></tr></thead>
            <tbody id="top10-table"></tbody></table>
          </div>
        </div>
      </div>
    </div>

    <div class="view" id="logs-view">
      <div class="grid two">
        <div class="card">
          <div class="section-title">Recent logs</div>
          <div class="logs" id="entries"><div class="muted">Loading&hellip;</div></div>
        </div>
        <div class="card">
          <div class="section-title">Latest 50</div>
          <div class="table-wrap">
            <table><thead><tr><th>Timestamp</th><th>Source</th><th>Command</th><th>Exit</th></tr></thead>
            <tbody id="stats-last50"></tbody></table>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
const esc = s => String(s??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = new Intl.DateTimeFormat('hu-HU',{timeZone:'Europe/Budapest',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});

function formatTs(v){
  if(!v) return '';
  const raw = String(v).trim();
  const hasTz = /([zZ]|[+-]\d{2}:?\d{2})$/.test(raw);
  const norm = raw.includes('T')?(hasTz?raw:`${raw}Z`):`${raw.replace(' ','T')}Z`;
  const d = new Date(norm);
  if(isNaN(d)) return raw;
  const p = Object.fromEntries(fmt.formatToParts(d).map(x=>[x.type,x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}
function short(v,n=220){ const t=String(v??'').trim(); return t?t.length>n?t.slice(0,n)+'…':t:'—'; }
function statusCls(s){ const x=String(s||'').toLowerCase(); return ['idle','executing','down'].includes(x)?x:'unknown'; }

/* ── MCP health (smooth – only update DOM when something changes) ── */
let _lastMcpKey = '';
function setMcpState(data){
  const state = statusCls(data?.state);
  const reachable = !!data?.reachable;
  const cmd = data?.current_command||'';
  const key = `${state}|${reachable}|${cmd}`;

  const visual  = document.getElementById('mcp-visual');
  const stateEl = document.getElementById('mcp-state');
  const metaEl  = document.getElementById('mcp-meta');
  const noteEl  = document.getElementById('monitor-note');
  const pill    = document.getElementById('monitor-pill');
  const lblMap  = {idle:'idle',executing:'executing',down:'down',unknown:'unknown'};

  const metaTxt = reachable
    ? `${data.health_url||''} · updated ${data.updated_at?formatTs(data.updated_at):'now'}`
    : `${data.health_url||''} · unavailable`;
  const noteTxt = reachable
    ? (state==='executing'?`executing: ${short(cmd,60)}`:'healthy')
    : (data?.error||'not reachable');

  // Heavy DOM only when key changes
  if(key !== _lastMcpKey){
    _lastMcpKey = key;
    visual.dataset.state = state;
    stateEl.textContent = reachable ? lblMap[state] : 'down';
    pill.className = `pill ${state}`;
    pill.innerHTML = `<span class="dot"></span>${esc(reachable?lblMap[state]:'down')}`;
    noteEl.textContent = noteTxt;
  }
  // Timestamp always updates quietly
  metaEl.textContent = metaTxt;
}

async function loadMcpHealth(){
  try{
    const r = await fetch('/mcp-health',{cache:'no-store',credentials:'include'});
    setMcpState(await r.json());
  } catch(e){
    setMcpState({reachable:false,state:'down',error:String(e),health_url:'__VPS_MCP_URL__/health'});
  }
}

/* ── Logs (incremental – never re-render existing cards) ── */
let _logIds = new Map(); // id → element

function logCardHTML(row, isNew){
  const exit = Number(row.exit_code??row.code??0);
  return `<article class="log-card${isNew?' log-new':''}" id="lc-${row.id}">
    <div class="log-head">
      <div class="mono">${esc(formatTs(row.created_at))}</div>
      <div class="pill ${String(row.source||'').toLowerCase()==='user'?'idle':'unknown'}"><span class="dot"></span>${esc(row.source||'agent')}</div>
      <div class="mono log-summary">${esc(short(row.cmd,120))}</div>
      <div class="pill ${exit===0?'idle':'down'}"><span class="dot"></span>exit ${esc(exit)}</div>
    </div>
    <div class="log-cmd mono">${esc(row.cmd||'')}</div>
    <div style="margin-top:10px"><pre class="mono">${esc(short(row.stdout||'',280))}</pre></div>
    ${exit!==0?`<div style="margin-top:10px"><pre class="mono">${esc(short(row.stderr||'',280))}</pre></div>`:''}
  </article>`;
}

function applyLogs(rows){
  const box = document.getElementById('entries');
  if(!rows||!rows.length){
    if(_logIds.size>0){ box.innerHTML='<div class="muted">No logs</div>'; _logIds.clear(); }
    return;
  }
  // Remove any "loading" placeholder on first run
  const placeholder = box.querySelector('.muted');
  if(placeholder && _logIds.size===0) placeholder.remove();

  const incoming = new Map(rows.map(r=>[r.id, r]));

  // Remove stale entries (scrolled off the limit)
  for(const [id, el] of _logIds){
    if(!incoming.has(id)){ el.remove(); _logIds.delete(id); }
  }
  // Prepend new entries (sorted newest-first, so iterate in order)
  const newRows = rows.filter(r=>!_logIds.has(r.id));
  if(newRows.length){
    const frag = document.createDocumentFragment();
    for(const row of newRows){
      const tmp = document.createElement('div');
      tmp.innerHTML = logCardHTML(row, true);
      const el = tmp.firstElementChild;
      frag.appendChild(el);
      _logIds.set(row.id, el);
    }
    box.insertBefore(frag, box.firstChild);
  }
}

let _logsSeq = 0, _logsCtrl = null;
async function loadLogs(){
  const seq = ++_logsSeq;
  if(_logsCtrl) try{ _logsCtrl.abort(); }catch(_){}
  _logsCtrl = new AbortController();
  const url = `/logs?source=all&q=&limit=20&errors_only=0&_=${Date.now()}`;
  try{
    const r = await fetch(url,{cache:'no-store',credentials:'include',signal:_logsCtrl.signal});
    if(seq!==_logsSeq) return;
    if(!r.ok) return;
    const data = await r.json();
    applyLogs(Array.isArray(data)?data:[]);
  } catch(e){ if(e?.name==='AbortError') return; }
}

/* ── Stats (update values in-place, only rebuild table when data changes) ── */
let _lastTopKey='', _lastLast50Key='';
async function loadStats(){
  try{
    const r = await fetch('/stats',{cache:'no-store',credentials:'include'});
    const s = await r.json();
    document.getElementById('total-errors').textContent = s.total_errors??0;
    document.getElementById('top10-count').textContent = (s.top_commands||[]).length;
    document.getElementById('last50-count').textContent = (s.last_50||[]).length;

    const topKey = JSON.stringify(s.top_commands);
    if(topKey!==_lastTopKey){
      _lastTopKey=topKey;
      document.getElementById('top10-table').innerHTML =
        (s.top_commands||[]).map(r=>`<tr><td class="mono">${esc(short(r.cmd,110))}</td><td>${esc(r.count)}</td></tr>`).join('')||
        '<tr><td colspan="2" class="muted">No data</td></tr>';
    }
    const l50Key = JSON.stringify((s.last_50||[]).map(r=>r.id));
    if(l50Key!==_lastLast50Key){
      _lastLast50Key=l50Key;
      document.getElementById('stats-last50').innerHTML =
        (s.last_50||[]).map(r=>{
          const exit=Number(r.exit_code??r.code??0);
          return `<tr><td class="mono">${esc(formatTs(r.created_at))}</td><td>${esc(r.source||'agent')}</td><td class="mono">${esc(short(r.cmd,110))}</td><td>${exit===0?'0':esc(exit)}</td></tr>`;
        }).join('')||'<tr><td colspan="4" class="muted">No data</td></tr>';
    }
  } catch(_){}
}

/* ── Tabs ── */
function activateTab(name){
  document.querySelectorAll('.view').forEach(el=>el.classList.toggle('active',el.id===`${name}-view`));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  if(name==='logs'){ loadStats(); loadLogs(); }
}
document.querySelectorAll('.tab-btn').forEach(b=>b.addEventListener('click',()=>activateTab(b.dataset.tab)));

/* ── Exec form ── */
document.getElementById('exec-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Running…';
  const payload = {cmd:document.getElementById('cmd').value, source:document.getElementById('source-input').value||'user'};
  try{
    const r = await fetch('/exec',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d = await r.json();
    document.getElementById('last-result').style.display='block';
    document.getElementById('result-code').textContent = d.code??d.exit_code??'?';
    document.getElementById('exec-status').textContent = 'completed · status: '+(d.status||'done');
    document.getElementById('result-stdout').textContent = d.stdout||'';
    document.getElementById('result-stderr').textContent = d.stderr||'';
    loadStats(); loadLogs();
  } finally{
    btn.disabled=false; btn.textContent='Run';
  }
});

/* ── Kill / Start switch ── */
let _svcBusy = false;
async function serviceAction(action){
  if(_svcBusy) return;
  _svcBusy = true;
  const killBtn  = document.getElementById('btn-kill');
  const startBtn = document.getElementById('btn-start');
  killBtn.disabled = true; startBtn.disabled = true;
  const label = action==='stop'?'Killing…':'Starting…';
  (action==='stop'?killBtn:startBtn).textContent = label;
  try{
    const r = await fetch(`/service/${action}`,{method:'POST',credentials:'include'});
    const d = await r.json();
    if(!d.ok) console.warn('service action failed', d);
    // Poll health quickly after action
    await new Promise(res=>setTimeout(res,800));
    await loadMcpHealth();
  } catch(e){ console.error(e); }
  finally{
    _svcBusy = false;
    killBtn.disabled = false; startBtn.disabled = false;
    killBtn.textContent = 'Kill'; startBtn.textContent = 'Start';
  }
}
document.getElementById('btn-kill').addEventListener('click', ()=>serviceAction('stop'));
document.getElementById('btn-start').addEventListener('click', ()=>serviceAction('start'));

/* ── Boot ── */
loadMcpHealth();
loadStats();
loadLogs();
setInterval(loadMcpHealth, 2000);
setInterval(()=>{
  loadStats();
  if(document.getElementById('logs-view').classList.contains('active')) loadLogs();
}, 8000);
</script>
</body>
</html>"""


@app.get('/', response_class=HTMLResponse)
def index(_: str = Depends(auth)) -> HTMLResponse:
    html = HTML.replace('__PORT__', str(PORT)).replace('__VPS_MCP_URL__', VPS_MCP_URL)
    return HTMLResponse(html)


@app.get('/stats')
def stats(_: str = Depends(auth)) -> JSONResponse:
    return JSONResponse(query_stats())


@app.get('/logs')
def logs(source: str = 'all', q: str = '', limit: int = 100, errors_only: bool = False, _: str = Depends(auth)) -> JSONResponse:
    return JSONResponse(query_logs(limit=limit, source=source, q=q, errors_only=errors_only))


@app.get('/logs/{entry_id}')
def logs_by_id(entry_id: int, _: str = Depends(auth)) -> JSONResponse:
    return JSONResponse(query_log_by_id(entry_id))


@app.post('/exec')
async def exec_cmd(payload: dict[str, Any], _: str = Depends(auth)) -> JSONResponse:
    cmd = str(payload.get('cmd') or '').strip()
    if not cmd:
        raise HTTPException(status_code=400, detail='cmd is required')
    source = str(payload.get('source') or 'user').strip() or 'user'
    try:
        result = _vps_request('POST', '/exec', {'cmd': cmd, 'source': source}, timeout=310.0)
        return JSONResponse(result)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f'vps-mcp unreachable: {exc}')


@app.post('/service/{action}')
def service_control(action: str, _: str = Depends(auth)) -> JSONResponse:
    if action not in ('stop', 'start', 'restart'):
        raise HTTPException(status_code=400, detail='invalid action')
    result = subprocess.run(
        ['sudo', 'systemctl', action, 'vps-mcp'],
        capture_output=True, text=True, timeout=15,
    )
    return JSONResponse({'ok': result.returncode == 0, 'stdout': result.stdout, 'stderr': result.stderr})


@app.get('/mcp-health')
def mcp_health_endpoint(_: str = Depends(auth)) -> JSONResponse:
    return JSONResponse(mcp_health())
