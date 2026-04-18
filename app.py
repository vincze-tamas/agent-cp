from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

BASE = Path('/opt/agent-cp')
LOG_DB = BASE / 'log.db'
LOCAL_DB = LOG_DB
OPT_DB = LOG_DB
COMMANDS_DB = OPT_DB
PORT = int(os.getenv('PORT', '8091'))
MCP_HEALTH_URL = 'http://localhost:8093/health'


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

app = FastAPI(docs_url=None, redoc_url=None)
basic = HTTPBasic()


def conn(db_path: Path | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path or LOG_DB))
    c.row_factory = sqlite3.Row
    return c


def ensure_schema() -> None:
    COMMANDS_DB.parent.mkdir(parents=True, exist_ok=True)
    with conn(COMMANDS_DB) as c:
        c.execute(
            """
            create table if not exists commands (
                id integer primary key autoincrement,
                created_at text not null default current_timestamp,
                cmd text not null,
                stdout text not null default '',
                stderr text not null default '',
                code integer not null default 0,
                source text not null default 'agent'
            )
            """
        )
        cols = {row['name'] for row in c.execute('pragma table_info(commands)').fetchall()}
        if 'created_at' not in cols:
            c.execute("alter table commands add column created_at text not null default current_timestamp")
        if 'source' not in cols:
            c.execute("alter table commands add column source text not null default 'agent'")
        if 'stdout' not in cols:
            c.execute("alter table commands add column stdout text not null default ''")
        if 'stderr' not in cols:
            c.execute("alter table commands add column stderr text not null default ''")
        c.commit()

    BASE.mkdir(parents=True, exist_ok=True)
    with conn(LOCAL_DB) as c:
        c.execute(
            """
            create table if not exists exec_log (
                id integer primary key autoincrement,
                ts text default current_timestamp,
                cmd text not null,
                stdout text not null default '',
                stderr text not null default '',
                code integer not null default 0,
                source text not null default 'agent'
            )
            """
        )
        cols = {row['name'] for row in c.execute('pragma table_info(exec_log)').fetchall()}
        if 'source' not in cols:
            c.execute("alter table exec_log add column source text not null default 'agent'")
        if 'stdout' not in cols:
            c.execute("alter table exec_log add column stdout text not null default ''")
        if 'stderr' not in cols:
            c.execute("alter table exec_log add column stderr text not null default ''")
        c.commit()


def auth(creds: HTTPBasicCredentials = Depends(basic)) -> str:
    if not (creds.username == CP_USER and creds.password == CP_PASS):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={'WWW-Authenticate': 'Basic realm=agent-cp'})
    return creds.username


def run_cmd(cmd: str) -> tuple[int, str, str]:
    completed = subprocess.run(cmd, shell=True, executable='/bin/bash', capture_output=True, text=True, timeout=120)
    return completed.returncode, completed.stdout or '', completed.stderr or ''


def normalize_source(value: Any) -> str:
    raw = str(value or 'all').strip().lower()
    if raw in {'', 'all', 'any', '*'}:
        return 'all'
    if raw in {'ui', 'user'}:
        return 'user'
    if raw == 'agent':
        return 'agent'
    return 'all'


def analytics_db_path() -> Path:
    return LOG_DB


def open_table(db_path: Path) -> tuple[sqlite3.Connection, str, set[str]]:
    c = conn(db_path)
    tables = {row['name'] for row in c.execute("select name from sqlite_master where type='table'").fetchall()}
    for table in ('commands', 'exec_log', 'history'):
        if table in tables:
            cols = {row['name'] for row in c.execute(f'pragma table_info({table})').fetchall()}
            return c, table, cols
    c.close()
    raise RuntimeError(f'no log table found in {db_path}')


def cmd_expr(cols: set[str]) -> str:
    if 'cmd' in cols:
        return 'cmd'
    if 'command' in cols:
        return 'command'
    return "''"


def stdout_expr(cols: set[str]) -> str:
    if 'stdout' in cols:
        return 'stdout'
    if 'output' in cols:
        return 'output'
    return "''"


def stderr_expr(cols: set[str]) -> str:
    if 'stderr' in cols:
        return 'stderr'
    return "''"


def exit_expr(cols: set[str]) -> str:
    if 'exit_code' in cols:
        return 'exit_code'
    if 'code' in cols:
        return 'code'
    return '0'


def ts_expr(cols: set[str]) -> str:
    if 'created_at' in cols:
        return 'created_at'
    if 'ts' in cols:
        return 'ts'
    return 'coalesce(created_at, ts)'


def select_row_sql(cols: set[str], truncated: bool) -> str:
    cmd_e = cmd_expr(cols)
    stdout_e = stdout_expr(cols)
    stderr_e = stderr_expr(cols)
    exit_e = exit_expr(cols)
    ts_e = ts_expr(cols)
    out_stdout = f"substr(coalesce({stdout_e}, ''), 1, 280)" if truncated else f"coalesce({stdout_e}, '')"
    out_stderr = f"substr(coalesce({stderr_e}, ''), 1, 280)" if truncated else f"coalesce({stderr_e}, '')"
    return f"""
        select
            id,
            {ts_e} as created_at,
            {cmd_e} as cmd,
            {out_stdout} as stdout,
            {out_stderr} as stderr,
            coalesce({exit_e}, 0) as exit_code,
            coalesce({exit_e}, 0) as code,
            case
                when lower(coalesce(source, 'agent')) in ('ui', 'user') then 'user'
                else 'agent'
            end as source
    """


def build_where(cols: set[str], source: str = 'all', q: str = '', errors_only: bool = False) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    source_n = normalize_source(source)
    if source_n in {'agent', 'user'}:
        where.append("case when lower(coalesce(source, 'agent')) in ('ui', 'user') then 'user' else 'agent' end = ?")
        params.append(source_n)
    if q:
        where.append(f"lower(coalesce({cmd_expr(cols)}, '')) like lower(?)")
        params.append(f'%{q}%')
    if errors_only:
        where.append("CAST(COALESCE(code, 0) AS INTEGER) != 0")
    return (' where ' + ' and '.join(where)) if where else '', params


def query_logs(limit: int = 100, source: str = 'all', q: str = '', errors_only: bool = False) -> list[dict[str, Any]]:
    c, table, cols = open_table(analytics_db_path())
    try:
        where_sql, params = build_where(cols, source=source, q=q, errors_only=errors_only)
        sql = select_row_sql(cols, truncated=True) + f' from {table}' + where_sql + ' order by id desc limit ?'
        params.append(max(1, min(int(limit or 100), 500)))
        return [dict(r) for r in c.execute(sql, params).fetchall()]
    finally:
        c.close()


def query_log_by_id(entry_id: int) -> dict[str, Any]:
    c, table, cols = open_table(analytics_db_path())
    try:
        row = c.execute(select_row_sql(cols, truncated=False) + f' from {table} where id = ? limit 1', (entry_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='log not found')
        return dict(row)
    finally:
        c.close()


def query_stats() -> dict[str, Any]:
    c, table, cols = open_table(analytics_db_path())
    try:
        cmd_e = cmd_expr(cols)
        exit_e = exit_expr(cols)
        last50 = c.execute(select_row_sql(cols, truncated=True) + f' from {table} order by id desc limit 50').fetchall()
        total_errors = c.execute(f'select count(*) as n from {table} where coalesce({exit_e}, 0) != 0').fetchone()['n']
        top_cmds = c.execute(f"select {cmd_e} as cmd, count(*) as count from {table} group by {cmd_e} order by count desc, cmd asc limit 10").fetchall()
        return {
            'db_path': str(analytics_db_path()),
            'last_50': [dict(r) for r in last50],
            'total_errors': int(total_errors or 0),
            'top_commands': [dict(r) for r in top_cmds],
        }
    finally:
        c.close()


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


HTML = """<!doctype html>
<html lang=\"hu\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">
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
      margin: 0;
      min-height: 100vh;
      width: 100%;
      max-width: 100vw;
      overflow-x: hidden;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(110,140,255,0.18), transparent 30%),
        radial-gradient(circle at top right, rgba(34,197,94,0.10), transparent 24%),
        var(--bg);
    }
    .page { max-width: 1400px; width: min(100%, 1400px); margin: 0 auto; padding: 18px 14px 32px; overflow-x: hidden; }
    .shell { display: grid; gap: 14px; grid-template-columns: 1fr; min-width: 0; max-width: 100%; }
    .header, .card {
      min-width: 0;
      max-width: 100%;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 18px 42px rgba(0,0,0,0.24);
    }
    .header {
      display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
      padding: 16px 18px;
    }
    .brand { display: grid; gap: 4px; }
    h1 { margin: 0; font-size: 24px; letter-spacing: -0.02em; }
    .sub { color: var(--muted); font-size: 13px; }
    .status-hero {
      display: flex; align-items: center; gap: 14px; padding: 12px 14px; border-radius: 16px;
      background: rgba(8, 13, 25, 0.76); border: 1px solid var(--border); max-width: 100%; min-width: 0;
    }
    .status-visual {
      --accent: #b66cff;
      --accent-soft: rgba(182, 108, 255, 0.32);
      --accent-hot: rgba(231, 203, 255, 0.95);
      --glow: rgba(182, 108, 255, 0.34);
      --wave-speed: 4.8s;
      width: 82px;
      height: 82px;
      flex: 0 0 auto;
      position: relative;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background:
        radial-gradient(circle at 50% 50%, rgba(255,255,255,0.16), rgba(255,255,255,0.0) 28%),
        radial-gradient(circle at 50% 50%, rgba(182,108,255,0.20), rgba(10,15,28,0.0) 66%);
      box-shadow:
        0 0 0 1px rgba(255,255,255,0.03) inset,
        0 0 28px var(--glow),
        0 0 58px rgba(121, 69, 255, 0.16);
      overflow: hidden;
      color: var(--accent);
      isolation: isolate;
    }
    .status-visual::before,
    .status-visual::after {
      content: '';
      position: absolute;
      inset: 8px;
      border-radius: 50%;
      pointer-events: none;
    }
    .status-visual::before {
      background:
        radial-gradient(circle at 50% 42%, rgba(255,255,255,0.26), rgba(255,255,255,0.03) 18%, rgba(255,255,255,0.0) 42%),
        radial-gradient(circle at 50% 58%, rgba(255,255,255,0.14), rgba(255,255,255,0.0) 34%);
      filter: blur(1px);
      opacity: .9;
      mix-blend-mode: screen;
    }
    .status-visual::after {
      inset: -10px;
      border: 1px solid rgba(255,255,255,0.08);
      box-shadow: inset 0 0 30px rgba(255,255,255,0.05);
      opacity: .35;
      filter: blur(.4px);
    }
    .status-svg { width: 100%; height: 100%; display: block; }
    .status-wave {
      fill: none;
      stroke: currentColor;
      stroke-linecap: round;
      opacity: .72;
      transform-origin: 50% 50%;
      filter: drop-shadow(0 0 8px currentColor);
    }
    .status-wave.wave-1 { stroke-width: 1.5; stroke-dasharray: 18 9; animation: waveSpin var(--wave-speed) linear infinite; }
    .status-wave.wave-2 { stroke-width: 1.2; stroke-dasharray: 9 12; opacity: .42; animation: waveSpin calc(var(--wave-speed) * 1.36) linear infinite reverse; }
    .status-wave.wave-3 { stroke-width: 1.0; stroke-dasharray: 4 15; opacity: .22; animation: waveSpin calc(var(--wave-speed) * 1.8) linear infinite; }
    .status-halo {
      fill: url(#halo-gradient);
      opacity: .95;
      mix-blend-mode: screen;
      animation: haloPulse 4.6s ease-in-out infinite;
    }
    .status-core {
      fill: url(#core-gradient);
      transform-origin: 50% 50%;
      animation: corePulse 3.8s ease-in-out infinite;
      filter: url(#status-blur);
    }
    .status-spark {
      fill: rgba(255,255,255,0.95);
      opacity: .82;
      filter: drop-shadow(0 0 8px rgba(255,255,255,0.95));
      animation: sparkPulse 2.8s ease-in-out infinite;
    }
    .status-visual[data-state='idle'] {
      --accent: #bd77ff;
      --accent-soft: rgba(189, 119, 255, 0.32);
      --accent-hot: rgba(247, 235, 255, 0.92);
      --glow: rgba(168, 85, 247, 0.42);
      --wave-speed: 6.4s;
      filter: saturate(1.05);
    }
    .status-visual[data-state='executing'] {
      --accent: #ffb357;
      --accent-soft: rgba(255, 179, 87, 0.30);
      --accent-hot: rgba(255, 250, 241, 1);
      --glow: rgba(255, 170, 64, 0.52);
      --wave-speed: 2.3s;
      filter: saturate(1.25) brightness(1.06);
    }
    .status-visual[data-state='down'] {
      --accent: #ff5f74;
      --accent-soft: rgba(255, 95, 116, 0.24);
      --accent-hot: rgba(255, 224, 228, 0.88);
      --glow: rgba(255, 95, 116, 0.42);
      --wave-speed: 3.8s;
      filter: saturate(1.22) brightness(0.95);
      animation: statusFlicker 2.4s steps(1, end) infinite;
    }
    .status-text { display: grid; gap: 2px; min-width: 0; }
    .status-title { font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
    .status-line { font-size: 15px; font-weight: 700; }
    .status-meta { color: var(--muted); font-size: 12px; }
    @keyframes waveSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
    @keyframes haloPulse {
      0%, 100% { opacity: .72; transform: scale(0.98); }
      50% { opacity: 1; transform: scale(1.02); }
    }
    @keyframes corePulse {
      0%, 100% { transform: scale(0.94); opacity: .84; }
      50% { transform: scale(1.04); opacity: 1; }
    }
    @keyframes sparkPulse {
      0%, 100% { opacity: .62; transform: translateY(-1px) scale(0.94); }
      50% { opacity: 1; transform: translateY(0) scale(1.08); }
    }
    @keyframes statusFlicker {
      0%, 100% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 28px rgba(255,95,116,0.42), 0 0 58px rgba(255,95,116,0.15); }
      50% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 20px rgba(255,95,116,0.24), 0 0 38px rgba(255,95,116,0.08); }
      52% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 34px rgba(255,95,116,0.52), 0 0 68px rgba(255,95,116,0.18); }
      70% { box-shadow: 0 0 0 1px rgba(255,255,255,0.03) inset, 0 0 24px rgba(255,95,116,0.32), 0 0 50px rgba(255,95,116,0.12); }
    }

    .grid.two { grid-template-columns: 1fr; }
    .card { padding: 16px; overflow-x: hidden; }
    .section-title { margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; font-size: 12px; }
    .monitor { display: grid; gap: 10px; }
    .monitor-row {
      display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr) auto; gap: 10px; align-items: center;
      padding: 12px 0; border-bottom: 1px solid rgba(148,163,184,0.12); min-width: 0;
    }
    .monitor-row > * { min-width: 0; }
    .monitor-row:last-child { border-bottom: none; }
    .monitor-name { font-weight: 700; }
    .monitor-url, .monitor-note, .mono, input, textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .monitor-url, .monitor-note { color: var(--muted); font-size: 12px; word-break: break-word; overflow-wrap: anywhere; }
    .pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 11px; border-radius: 999px; border: 1px solid var(--border); background: rgba(8, 13, 25, 0.62); font-size: 12px; font-weight: 700; white-space: nowrap; }
    .pill .dot { width: 8px; height: 8px; border-radius: 999px; background: currentColor; }
    .pill.idle { color: var(--ok); }
    .pill.executing { color: var(--warn); }
    .pill.down { color: var(--down); }
    .pill.unknown { color: #94a3b8; }
    .content { display: grid; gap: 14px; grid-template-columns: 1fr; min-width: 0; max-width: 100%; }
    .panel { display: grid; gap: 14px; min-width: 0; max-width: 100%; }
    .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; min-width: 0; max-width: 100%; }
    .toolbar > * { flex: 1 1 auto; min-width: 0; max-width: 100%; }
    .toolbar .small { flex: 0 0 auto; }
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
    .stats { display: grid; gap: 10px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .stat { padding: 14px; border-radius: 14px; background: rgba(8,13,25,0.68); border: 1px solid var(--border); }
    .stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 6px; }
    .stat .value { font-size: 28px; font-weight: 800; }
    .table-wrap { overflow-x: auto; max-width: 100%; -webkit-overflow-scrolling: touch; }
    table { width: 100%; max-width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { text-align: left; border-bottom: 1px solid rgba(148,163,184,0.12); padding: 10px 8px; vertical-align: top; word-break: break-word; overflow-wrap: anywhere; min-width: 0; }
    th { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }
    .logs { display: grid; gap: 12px; }
    .log-card { padding: 14px; border-radius: 16px; background: linear-gradient(180deg, rgba(16,23,39,0.98), rgba(12,18,32,0.98)); border: 1px solid var(--border); min-width: 0; overflow-x: hidden; }
    .log-head { display: grid; gap: 8px; grid-template-columns: minmax(0, 180px) minmax(0, auto) minmax(0, 1fr) auto; align-items: center; }
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
      .toolbar > *, .toolbar input { flex-basis: 100%; }
      .page { padding-left: 12px; padding-right: 12px; }
    }
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"header\">
      <div class=\"brand\">
        <h1>Agent Control Panel</h1>
        <div class=\"sub\">port __PORT__ · minimal mobile-first dashboard with live MCP state</div>
      </div>
      <div class="status-hero">
        <div class="status-visual" id="mcp-visual" data-state="unknown" aria-hidden="true">
          <svg class="status-svg" viewBox="0 0 120 120" role="img" aria-label="MCP status indicator">
            <defs>
              <filter id="status-blur" x="-40%" y="-40%" width="180%" height="180%">
                <feGaussianBlur stdDeviation="1.8" />
              </filter>
              <radialGradient id="halo-gradient" cx="50%" cy="42%" r="62%">
                <stop offset="0%" stop-color="var(--accent-hot)" stop-opacity="0.95" />
                <stop offset="34%" stop-color="var(--accent)" stop-opacity="0.34" />
                <stop offset="70%" stop-color="var(--accent-soft)" stop-opacity="0.14" />
                <stop offset="100%" stop-color="rgba(0,0,0,0)" stop-opacity="0" />
              </radialGradient>
              <radialGradient id="core-gradient" cx="50%" cy="42%" r="62%">
                <stop offset="0%" stop-color="var(--accent-hot)" stop-opacity="1" />
                <stop offset="18%" stop-color="var(--accent)" stop-opacity="0.96" />
                <stop offset="48%" stop-color="var(--accent-soft)" stop-opacity="0.72" />
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
          <div class="status-title">MCP state</div>
          <div class="status-line" id="mcp-state">checking…</div>
          <div class="status-meta" id="mcp-meta">polling http://localhost:8093/health</div>
        </div>
      </div>
    </div>

    <div class=\"card\">
      <div class=\"section-title\">Monitoring</div>
      <div class=\"monitor\" id=\"monitor-list\">
        <div class=\"monitor-row\">
          <div>
            <div class=\"monitor-name\">Dashboard API</div>
            <div class=\"monitor-url\">/exec, /stats, /logs, /mcp-health</div>
          </div>
          <div class=\"monitor-note\">Local control surface</div>
          <div class=\"pill idle\"><span class=\"dot\"></span>ready</div>
        </div>
        <div class="monitor-row">
          <div>
            <div class="monitor-name">MCP server</div>
            <div class="monitor-url">http://localhost:8093/health</div>
          </div>
          <div class="monitor-note" id="monitor-note">polling…</div>
          <div class="pill unknown" id="monitor-pill"><span class="dot"></span>unknown</div>
        </div>
      </div>
    </div>

    <div class="tabbar">
      <button class="tab-btn active" data-tab="exec" type="button">Exec</button>
      <button class="tab-btn" data-tab="logs" type="button">Logs</button>
    </div>


    <div class=\"view active\" id=\"exec-view\">
      <div class=\"content\">
        <div class=\"card\">
          <div class=\"section-title\">Run command</div>
          <form id=\"exec-form\">
            <textarea id=\"cmd\" name=\"cmd\" placeholder=\"Enter a shell command\"></textarea>
            <div class=\"exec-row\">
              <input id=\"source-input\" name=\"source\" value=\"user\" placeholder=\"source\" />
              <button type=\"submit\">Run</button>
            </div>
          </form>
          <div id=\"last-result\" class=\"result\" style=\"display:none\">
            <div class=\"muted\">exit code <span id=\"result-code\"></span></div>
            <div class=\"muted\" id=\"exec-status\" style=\"margin-top:4px\"></div>
            <div style=\"margin-top:10px\">
              <div class=\"section-title\" style=\"margin-bottom:8px\">stdout</div>
              <pre id=\"result-stdout\"></pre>
            </div>
            <div style=\"margin-top:10px\">
              <div class=\"section-title\" style=\"margin-bottom:8px\">stderr</div>
              <pre id=\"result-stderr\"></pre>
            </div>
          </div>
        </div>

        <div class=\"card\">
          <div class=\"section-title\">Stats</div>
          <div class=\"stats\">
            <div class=\"stat\">
              <div class=\"label\">Total errors</div>
              <div class=\"value\" id=\"total-errors\">0</div>
            </div>
            <div class=\"stat\">
              <div class=\"label\">Last 50 commands</div>
              <div class=\"value\" id=\"last50-count\">0</div>
            </div>
            <div class=\"stat\">
              <div class=\"label\">Top commands</div>
              <div class=\"value\" id=\"top10-count\">0</div>
            </div>
          </div>
          <div style=\"margin-top:14px\" class=\"table-wrap\">
            <table>
              <thead><tr><th>Command</th><th>Count</th></tr></thead>
              <tbody id=\"top10-table\"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <div class=\"view\" id=\"logs-view\">
      <div class=\"grid two\">
        <div class=\"card\">
          <div class=\"section-title\">Recent logs</div>
          <div class=\"logs\" id=\"entries\"></div>
        </div>
        <div class=\"card\">
          <div class=\"section-title\">Latest 50</div>
          <div class=\"table-wrap\">
            <table>
              <thead><tr><th>Timestamp</th><th>Source</th><th>Command</th><th>Exit</th></tr></thead>
              <tbody id=\"stats-last50\"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
const esc = (s) => String(s ?? '').replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));
let currentSource = 'all';
let currentQuery = '';
let currentErrorsOnly = false;
let logsRequestSeq = 0;
let logsAbortController = null;
const fmt = new Intl.DateTimeFormat('hu-HU', {
  timeZone: 'Europe/Budapest', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});

function statusClass(state) {
  const s = String(state || 'unknown').toLowerCase();
  return ['idle', 'executing', 'down'].includes(s) ? s : 'unknown';
}

function setMcpState(data) {
  const state = statusClass(data?.state);
  const visual = document.getElementById('mcp-visual');
  const stateEl = document.getElementById('mcp-state');
  const metaEl = document.getElementById('mcp-meta');
  const noteEl = document.getElementById('monitor-note');
  const pill = document.getElementById('monitor-pill');
  const labelMap = {idle: 'idle', executing: 'executing', down: 'down', unknown: 'unknown'};
  const meta = data?.reachable ? `${data.health_url || 'http://localhost:8093/health'} · updated ${data.updated_at ? formatTimestamp(data.updated_at) : 'now'}` : `${data.health_url || 'http://localhost:8093/health'} · unavailable`;
  visual.dataset.state = state;
  stateEl.textContent = data?.reachable ? labelMap[state] : 'down';
  metaEl.textContent = meta;
  noteEl.textContent = data?.reachable
    ? (state === 'executing' ? 'command in progress' : 'healthy')
    : (data?.error || 'not reachable');
  pill.className = `pill ${state}`;
  pill.innerHTML = `<span class=\"dot\"></span>${esc(data?.reachable ? labelMap[state] : 'down')}`;
}
function logCard(row) {
  const exitCode = Number(row.exit_code ?? row.code ?? 0);
  return `
    <article class="log-card">
      <div class="log-head">
        <div class="mono">${esc(formatTimestamp(row.created_at))}</div>
        <div class="pill ${esc(String(row.source || 'agent').toLowerCase() === 'user' ? 'idle' : 'unknown')}"><span class="dot"></span>${esc(row.source || 'agent')}</div>
        <div class="mono log-summary">${esc(shortText(row.cmd, 120))}</div>
        <div class="pill ${exitCode === 0 ? 'idle' : 'down'}"><span class="dot"></span>exit ${esc(exitCode)}</div>
      </div>
      <div class="log-cmd mono">${esc(row.cmd || '')}</div>
      <div style="margin-top:10px"><pre class="mono">${esc(shortText(row.stdout || '', 280))}</pre></div>
      ${exitCode !== 0 ? `<div style="margin-top:10px"><pre class="mono">${esc(shortText(row.stderr || '', 280))}</pre></div>` : ''}
    </article>`;
}


function statRow(row) {
  const exitCode = Number(row.exit_code ?? row.code ?? 0);
  return `<tr><td class=\"mono\">${esc(formatTimestamp(row.created_at))}</td><td>${esc(row.source || 'agent')}</td><td class=\"mono\">${esc(shortText(row.cmd, 110))}</td><td>${exitCode === 0 ? '0' : esc(exitCode)}</td></tr>`;
}

function topRow(row) {
  return `<tr><td class=\"mono\">${esc(shortText(row.cmd, 110))}</td><td>${esc(row.count)}</td></tr>`;
}

async function loadStats() {
  const res = await fetch('/stats', {cache: 'no-store', credentials: 'include'});
  const stats = await res.json();
  document.getElementById('total-errors').textContent = stats.total_errors ?? 0;
  document.getElementById('top10-count').textContent = (stats.top_commands || []).length;
  document.getElementById('last50-count').textContent = (stats.last_50 || []).length;
  document.getElementById('top10-table').innerHTML = (stats.top_commands || []).map(topRow).join('') || '<tr><td colspan=\"2\" class=\"muted\">No data</td></tr>';
  document.getElementById('stats-last50').innerHTML = (stats.last_50 || []).map(statRow).join('') || '<tr><td colspan=\"4\" class=\"muted\">No data</td></tr>';
}

async function loadLogs() {
  const requestId = ++logsRequestSeq;
  if (logsAbortController) {
    try { logsAbortController.abort(); } catch (_) {}
  }
  logsAbortController = new AbortController();
  const entries = document.getElementById('entries');
  if (!entries) return;
  entries.innerHTML = '<div class="muted">Loading…</div>';
  const url = '/logs?source=' + encodeURIComponent(currentSource)
    + '&q=' + encodeURIComponent(currentQuery)
    + '&limit=20&errors_only=' + (currentErrorsOnly ? '1' : '0')
    + '&_=' + Date.now();
  try {
    const res = await fetch(url, {cache: 'no-store', credentials: 'include', signal: logsAbortController.signal});
    const raw = await res.text();
    if (requestId !== logsRequestSeq) return;
    if (!res.ok) throw new Error('HTTP ' + res.status);
    let data = [];
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (_) {
        throw new Error('invalid JSON');
      }
    }
    const rows = Array.isArray(data) ? data
      : Array.isArray(data?.logs) ? data.logs
      : Array.isArray(data?.entries) ? data.entries
      : Array.isArray(data?.data) ? data.data
      : [];
    entries.innerHTML = rows.length ? rows.map(logCard).join('') : '<div class="muted">No logs</div>';
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    entries.innerHTML = '<div class="muted">Unable to load logs</div>';
  }
}


function activateTab(name) {
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('active', el.id === `${name}-view`));
  document.querySelectorAll('.tab-btn').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === name));
  if (name === 'logs') {
    loadStats();
    loadLogs();
  }
}

document.querySelectorAll('.tab-btn').forEach((btn) => btn.addEventListener('click', () => activateTab(btn.dataset.tab)));

document.getElementById('exec-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const payload = {
    cmd: document.getElementById('cmd').value,
    source: document.getElementById('source-input').value || 'user',
  };
  const res = await fetch('/exec', {method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const data = await res.json();
  const lastResult = document.getElementById('last-result');
  lastResult.style.display = 'block';
  document.getElementById('result-code').textContent = data.code;
  document.getElementById('exec-status').textContent = 'command completed';
  document.getElementById('result-stdout').textContent = data.stdout || '';
  document.getElementById('result-stderr').textContent = data.stderr || '';
  await loadStats();
});

loadMcpHealth();
loadStats();
loadLogs();
setInterval(loadMcpHealth, 2000);
setInterval(() => { if (document.getElementById('logs-view').classList.contains('active')) { loadStats(); loadLogs(); } }, 8000);
</script>
</body>
</html>"""


@app.on_event('startup')
def _startup() -> None:
    ensure_schema()


@app.get('/', response_class=HTMLResponse)
def index(_: str = Depends(auth)) -> HTMLResponse:
    return HTMLResponse(HTML.replace('__PORT__', str(PORT)))


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
def exec_cmd(payload: dict[str, Any], _: str = Depends(auth)) -> JSONResponse:
    cmd = str(payload.get('cmd') or '').strip()
    if not cmd:
        raise HTTPException(status_code=400, detail='cmd is required')
    source = normalize_source(payload.get('source'))
    code, stdout, stderr = run_cmd(cmd)
    with conn(COMMANDS_DB) as c:
        c.execute(
            'insert into commands (cmd, stdout, stderr, code, source) values (?, ?, ?, ?, ?)',
            (cmd, stdout, stderr, int(code), source),
        )
        c.commit()
    return JSONResponse({'code': int(code), 'stdout': stdout, 'stderr': stderr, 'source': source})


@app.get('/mcp-health')
def mcp_health_endpoint(_: str = Depends(auth)) -> JSONResponse:
    return JSONResponse(mcp_health())


