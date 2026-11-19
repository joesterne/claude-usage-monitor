#!/usr/bin/env python3
"""
Claude Usage Monitor - a tiny local server that receives Claude usage events,
logs them to SQLite, and shows a live-updating dashboard with pie charts.

Standard library only (Python 3.9+). Settings come from .env (see .env.example). No pip install needed.

    python usage_monitor.py                 # start server on http://127.0.0.1:8765
    python usage_monitor.py --port 9000     # different port
    python usage_monitor.py send --model claude-opus-5-5 --input 1200 --output 340
    python usage_monitor.py demo            # post a few fake events to try it out

Anything that can make an HTTP POST can report usage:

    POST http://127.0.0.1:8765/usage
    {
      "model": "claude-opus-5-5",
      "usage": {"input_tokens": 1200, "output_tokens": 340,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5000},
      "message_id": "msg_...",      # optional, used to de-duplicate
      "session_id": "...",          # optional
      "source": "claude-code"       # optional free-text label
    }

The token fields may also sit at the top level instead of inside "usage",
and you can POST a JSON list of events in one request.
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import env  # noqa: F401  (loads .env settings into os.environ)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

# ---------------------------------------------------------------------------
# Pricing (USD per million tokens), Anthropic first-party API list prices as of
# Nov 2026. Cost shown in the dashboard is an ESTIMATE. If you are on a Claude
# subscription (Pro/Max) via Claude Code, you are not billed per token; treat
# the number as "API-equivalent cost". Edit freely.
#   input, output, cache_read  (cache writes are estimated at 1.25x input)
# ---------------------------------------------------------------------------
PRICING = {
    "claude-fable-5-2":  (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1":  (10.00, 50.00, 1.00),
    "claude-fable-5":    (10.00, 50.00, 1.00),
    "claude-opus-5-5":   (4.00, 20.00, 0.20),
    "claude-opus-5":     (5.00, 25.00, 0.50),
    "claude-opus-4-8":   (5.00, 25.00, 0.50),
    "claude-opus-4-7":   (5.00, 25.00, 0.50),
    "claude-opus-4-6":   (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5":   (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5":  (0.10, 0.50, 0.01),
    "claude-haiku-4-5":  (1.00, 5.00, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25


def price_for(model):
    """Find pricing for a model id, tolerating prefixes/suffixes like
    'anthropic.claude-opus-5-5', 'claude-opus-5-5[1m]' or date suffixes."""
    if not model:
        return None
    m = model.lower()
    # longest key first so 'claude-opus-5-5' wins over 'claude-opus-5'
    for key in sorted(PRICING, key=len, reverse=True):
        if key in m:
            return PRICING[key]
    return None


def estimate_cost(model, inp, out, cache_write, cache_read):
    p = price_for(model)
    if not p:
        return 0.0
    pin, pout, pread = p
    return (inp * pin + out * pout + cache_write * pin * CACHE_WRITE_MULTIPLIER
            + cache_read * pread) / 1_000_000


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at                 TEXT NOT NULL,
    event_time                  TEXT,
    source                      TEXT,
    session_id                  TEXT,
    message_id                  TEXT UNIQUE,
    model                       TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0,
    output_tokens               INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    cost_usd                    REAL NOT NULL DEFAULT 0,
    raw_json                    TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_usage_received ON usage_events(received_at);
CREATE INDEX IF NOT EXISTS idx_usage_event_time ON usage_events(event_time);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.version = 0                      # bumps on every write; SSE watches it
        self.changed = threading.Condition(self.lock)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def add(self, ev):
        """Insert one normalized event. If message_id already exists, keep the
        larger token counts (Claude Code writes a message's usage on several
        transcript lines as it streams)."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO usage_events (received_at, event_time, source, session_id,
                    message_id, model, input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens, cost_usd, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET
                    input_tokens = MAX(input_tokens, excluded.input_tokens),
                    output_tokens = MAX(output_tokens, excluded.output_tokens),
                    cache_creation_input_tokens = MAX(cache_creation_input_tokens, excluded.cache_creation_input_tokens),
                    cache_read_input_tokens = MAX(cache_read_input_tokens, excluded.cache_read_input_tokens),
                    cost_usd = MAX(cost_usd, excluded.cost_usd)
                """,
                (
                    ev["received_at"], ev["event_time"], ev["source"], ev["session_id"],
                    ev["message_id"], ev["model"], ev["input_tokens"], ev["output_tokens"],
                    ev["cache_creation_input_tokens"], ev["cache_read_input_tokens"],
                    ev["cost_usd"], ev["raw_json"],
                ),
            )
            self.conn.commit()
            self.version += 1
            self.changed.notify_all()

    def summary(self, since=None):
        where, args = "", ()
        if since:
            where, args = "WHERE COALESCE(event_time, received_at) >= ?", (since,)
        with self.lock:
            cur = self.conn.cursor()
            by_model = [dict(r) for r in cur.execute(f"""
                SELECT COALESCE(model,'unknown') AS model,
                       COUNT(*) AS requests,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
                       SUM(cache_read_input_tokens) AS cache_read_input_tokens,
                       SUM(cost_usd) AS cost_usd
                FROM usage_events {where}
                GROUP BY model ORDER BY cost_usd DESC, requests DESC""", args)]
            recent = [dict(r) for r in cur.execute(f"""
                SELECT COALESCE(event_time, received_at) AS received_at, source, model, input_tokens, output_tokens,
                       cache_creation_input_tokens, cache_read_input_tokens, cost_usd
                FROM usage_events {where}
                ORDER BY COALESCE(event_time, received_at) DESC, id DESC LIMIT 15""", args)]
        totals = {k: sum((m[k] or 0) for m in by_model) for k in (
            "requests", "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd")}
        return {"version": self.version, "totals": totals,
                "by_model": by_model, "recent": recent}


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize(obj):
    """Accept a few shapes and return a dict ready for Store.add()."""
    if not isinstance(obj, dict):
        raise ValueError("each event must be a JSON object")
    # Allow posting a whole Messages API response: {"id","model","usage",...}
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
    model = obj.get("model") or usage.get("model")
    inp = _int(usage.get("input_tokens"))
    out = _int(usage.get("output_tokens"))
    cw = _int(usage.get("cache_creation_input_tokens"))
    cr = _int(usage.get("cache_read_input_tokens"))
    if inp + out + cw + cr == 0:
        raise ValueError("event has no token counts")
    message_id = obj.get("message_id") or (obj.get("id") if str(obj.get("id", "")).startswith("msg") else None)
    cost = obj.get("cost_usd")
    cost = float(cost) if cost is not None else estimate_cost(model, inp, out, cw, cr)
    return {
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_time": obj.get("timestamp") or obj.get("event_time"),
        "source": obj.get("source") or "api",
        "session_id": obj.get("session_id"),
        "message_id": message_id,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "cost_usd": cost,
        "raw_json": json.dumps(obj)[:4000],
    }


# ---------------------------------------------------------------------------
# Claude Code hook installation (~/.claude/settings.json)
# ---------------------------------------------------------------------------
CLAUDE_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
HOOK_EVENTS = ("Stop", "SubagentStop", "PostToolUse")
HOOK_MARKERS = ("claude_code_hook", "Claude Usage Monitor")


def _is_ours(cmd):
    return any(m in (cmd or "") for m in HOOK_MARKERS)


def _read_settings():
    try:
        with open(CLAUDE_SETTINGS) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def hook_status(cmd):
    """'connected' if our exact command is on every event, 'other' if an older
    copy of our hook is there, 'none' otherwise."""
    try:
        hooks = _read_settings().get("hooks", {})
    except Exception:
        return "unreadable"
    cmds = {e: [h.get("command") for g in hooks.get(e, []) for h in g.get("hooks", [])]
            for e in HOOK_EVENTS}
    if all(cmd in c for c in cmds.values()):
        return "connected"
    if any(_is_ours(x) for c in cmds.values() for x in c):
        return "other"
    return "none"


def install_hook(cmd):
    """Add our hook to Claude Code settings, replacing older copies of it.
    Keeps every other setting and hook; writes a .bak first."""
    settings = _read_settings()
    if os.path.exists(CLAUDE_SETTINGS):
        with open(CLAUDE_SETTINGS) as src, open(CLAUDE_SETTINGS + ".bak", "w") as dst:
            dst.write(src.read())
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        groups = []
        for g in hooks.get(event, []):
            kept = [h for h in g.get("hooks", []) if not _is_ours(h.get("command"))]
            if kept:
                groups.append(dict(g, hooks=kept))
        entry = {"hooks": [{"type": "command", "command": cmd}]}
        if event == "PostToolUse":
            entry = {"matcher": "*", **entry}
        groups.append(entry)
        hooks[event] = groups
    os.makedirs(os.path.dirname(CLAUDE_SETTINGS), exist_ok=True)
    tmp = CLAUDE_SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, CLAUDE_SETTINGS)


def default_hook_command():
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_code_hook.py")
    return f'"{sys.executable}" "{script}"'


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    store = None         # set in make_server()
    hook_command = None  # command line Claude Code should run as the hook
    server_version = "ClaudeUsageMonitor/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("USAGE_MONITOR_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path == "/":
            self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif path == "/api/summary":
            self._send(200, json.dumps(self.store.summary(params.get("since"))))
        elif path == "/events":
            self._stream(params.get("since"))
        elif path == "/api/hook":
            self._send(200, json.dumps({"status": hook_status(self.hook_command)}))
        elif path == "/health":
            self._send(200, '{"ok": true}')
        else:
            self._send(404, '{"error": "not found"}')

    def do_POST(self):
        if self.path.split("?")[0] == "/api/refresh":
            # Re-scan Claude Code transcripts for anything the hook missed,
            # then let the dashboard reload. Same header guard as /api/hook.
            if self.headers.get("X-Usage-Monitor") != "1":
                return self._send(403, '{"error": "forbidden"}')
            try:
                import claude_code_hook
                host, port = self.server.server_address[:2]
                n = claude_code_hook.backfill(url=f"http://{host}:{port}/usage")
                return self._send(200, json.dumps({"imported": n}))
            except Exception as ex:
                return self._send(500, json.dumps({"error": str(ex)}))
        if self.path.split("?")[0] == "/api/hook":
            # custom header forces a CORS preflight, so other websites can't trigger this
            if self.headers.get("X-Usage-Monitor") != "1":
                return self._send(403, '{"error": "forbidden"}')
            try:
                install_hook(self.hook_command)
                return self._send(200, json.dumps({"status": hook_status(self.hook_command)}))
            except Exception as ex:
                return self._send(500, json.dumps({"error": str(ex)}))
        if self.path.split("?")[0] != "/usage":
            return self._send(404, '{"error": "not found"}')
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"null")
            events = payload if isinstance(payload, list) else [payload]
            accepted, errors = 0, []
            for e in events:
                try:
                    self.store.add(normalize(e))
                    accepted += 1
                except ValueError as ex:
                    errors.append(str(ex))
            self._send(200 if accepted or not errors else 400,
                       json.dumps({"accepted": accepted, "errors": errors}))
        except Exception as ex:  # bad JSON etc.
            self._send(400, json.dumps({"error": str(ex)}))

    def _stream(self, since):
        """Server-Sent Events: push a fresh summary whenever data changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = -1
        try:
            while True:
                with self.store.changed:
                    if self.store.version == last:
                        self.store.changed.wait(timeout=15)
                    current = self.store.version
                if current != last:
                    last = current
                    msg = "data: " + json.dumps(self.store.summary(since)) + "\n\n"
                else:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ---------------------------------------------------------------------------
# Dashboard (single page, no external dependencies; pie charts drawn on canvas)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Usage Monitor</title>
<style>
:root{--bg:#f7f6f3;--card:#fff;--fg:#1f1e1c;--muted:#6b6862;--line:#e6e3dc}
@media (prefers-color-scheme: dark){:root{--bg:#1a1917;--card:#24231f;--fg:#f0eee9;--muted:#a19d95;--line:#3a3833}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px}
header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
h1{font-size:20px;margin:0}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
background:#c0392b;margin-right:6px}.dot.live{background:#2e9e5b}
select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.stat .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}.stat .k{color:var(--muted);font-size:12px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px}
.chart h2,.tbl h2{font-size:14px;margin:0 0 8px}.pie{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
canvas{width:200px;height:200px}.legend{list-style:none;margin:0;padding:0;font-size:13px}
.legend li{display:flex;align-items:center;gap:6px;margin:3px 0}.sw{width:11px;height:11px;border-radius:3px;flex:none}
.legend .n{color:var(--muted);margin-left:auto;padding-left:10px;font-variant-numeric:tabular-nums}
.tbl{margin-top:10px;overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;font-variant-numeric:tabular-nums}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{color:var(--muted);font-weight:500}
.empty{color:var(--muted);padding:30px 0;text-align:center}
.tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ghost{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font:inherit;cursor:pointer}
.ghost:disabled{opacity:.6;cursor:default}
.hookbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:14px;flex-wrap:wrap}
.hookbar button{background:#d97757;color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
</style></head><body><main>
<header><h1><span id="dot" class="dot"></span>Claude Usage Monitor</h1>
<div class="tools"><button id="refresh" class="ghost" title="Import any new Claude Code usage and reload the charts">&#x21bb; Refresh</button>
<label>Range <select id="range"><option value="">All time</option><option value="1">Last hour</option>
<option value="24" selected>Last 24 hours</option><option value="168">Last 7 days</option></select></label></div></header>
<div id="hookbar" class="card hookbar" hidden><span id="hooktext"></span>
<button id="hookbtn">Connect Claude Code</button></div>
<section class="stats">
<div class="card stat"><div class="v" id="s-req">0</div><div class="k">Requests</div></div>
<div class="card stat"><div class="v" id="s-in">0</div><div class="k">Input tokens</div></div>
<div class="card stat"><div class="v" id="s-out">0</div><div class="k">Output tokens</div></div>
<div class="card stat"><div class="v" id="s-cache">0</div><div class="k">Cache read / write</div></div>
<div class="card stat"><div class="v" id="s-cost">$0.00</div><div class="k">Est. cost (API prices)</div></div>
</section>
<section class="charts">
<div class="card chart"><h2>Tokens by model</h2><div class="pie"><canvas id="c-model" width="400" height="400"></canvas><ul class="legend" id="l-model"></ul></div></div>
<div class="card chart"><h2>Tokens by type</h2><div class="pie"><canvas id="c-type" width="400" height="400"></canvas><ul class="legend" id="l-type"></ul></div></div>
<div class="card chart"><h2>Est. cost by model</h2><div class="pie"><canvas id="c-cost" width="400" height="400"></canvas><ul class="legend" id="l-cost"></ul></div></div>
</section>
<section class="card tbl"><h2>Latest events</h2><table><thead><tr><th>Time</th><th>Model</th><th>Input</th>
<th>Output</th><th>Cache write</th><th>Cache read</th><th>Cost</th></tr></thead><tbody id="recent"></tbody></table></section>
</main><script>
const COLORS=["#d97757","#6a9bcc","#788c5d","#c9a227","#9b6bb3","#4fa3a5","#c45a7a","#8a7f72"];
const fmt=n=>(n||0).toLocaleString(), usd=n=>"$"+(n||0).toFixed((n||0)<1?4:2);
const short=m=>(m||"unknown").replace(/^anthropic\./,"");
function pie(canvasId,legendId,items,valFmt){
  const c=document.getElementById(canvasId),ctx=c.getContext("2d"),W=c.width,R=W/2-6;
  ctx.clearRect(0,0,W,W);const total=items.reduce((a,i)=>a+i.v,0);
  const ul=document.getElementById(legendId);ul.innerHTML="";
  if(!total){ctx.fillStyle=getComputedStyle(document.body).getPropertyValue("--line");
    ctx.beginPath();ctx.arc(W/2,W/2,R,0,7);ctx.fill();ul.innerHTML='<li class="empty">No data yet</li>';return;}
  let a=-Math.PI/2;items.forEach((it,i)=>{const s=it.v/total*2*Math.PI;ctx.beginPath();ctx.moveTo(W/2,W/2);
    ctx.arc(W/2,W/2,R,a,a+s);ctx.closePath();ctx.fillStyle=COLORS[i%COLORS.length];ctx.fill();
    ctx.strokeStyle=getComputedStyle(document.body).getPropertyValue("--card");ctx.lineWidth=3;ctx.stroke();a+=s;
    const li=document.createElement("li");li.innerHTML=`<span class="sw" style="background:${COLORS[i%COLORS.length]}"></span>`+
      `<span></span><span class="n">${valFmt(it.v)} · ${(it.v/total*100).toFixed(1)}%</span>`;
    li.children[1].textContent=it.label;ul.appendChild(li);});
  ctx.globalCompositeOperation="destination-out";ctx.beginPath();ctx.arc(W/2,W/2,R*0.55,0,7);ctx.fill();
  ctx.globalCompositeOperation="source-over";}
function render(d){
  const t=d.totals;
  document.getElementById("s-req").textContent=fmt(t.requests);
  document.getElementById("s-in").textContent=fmt(t.input_tokens);
  document.getElementById("s-out").textContent=fmt(t.output_tokens);
  document.getElementById("s-cache").textContent=fmt(t.cache_read_input_tokens)+" / "+fmt(t.cache_creation_input_tokens);
  document.getElementById("s-cost").textContent=usd(t.cost_usd);
  const tok=m=>m.input_tokens+m.output_tokens+m.cache_creation_input_tokens+m.cache_read_input_tokens;
  pie("c-model","l-model",d.by_model.map(m=>({label:short(m.model),v:tok(m)})),fmt);
  pie("c-type","l-type",[{label:"Input",v:t.input_tokens},{label:"Output",v:t.output_tokens},
    {label:"Cache write",v:t.cache_creation_input_tokens},{label:"Cache read",v:t.cache_read_input_tokens}],fmt);
  pie("c-cost","l-cost",d.by_model.map(m=>({label:short(m.model),v:m.cost_usd||0})),usd);
  const tb=document.getElementById("recent");tb.innerHTML="";
  if(!d.recent.length){tb.innerHTML='<tr><td colspan="7" class="empty">Waiting for usage events…</td></tr>';}
  d.recent.forEach(r=>{const tr=document.createElement("tr");
    [new Date(r.received_at).toLocaleTimeString(),short(r.model),fmt(r.input_tokens),fmt(r.output_tokens),
     fmt(r.cache_creation_input_tokens),fmt(r.cache_read_input_tokens),usd(r.cost_usd)]
     .forEach(v=>{const td=document.createElement("td");td.textContent=v;tr.appendChild(td);});tb.appendChild(tr);});}
let es;
function connect(){
  if(es)es.close();const h=document.getElementById("range").value;
  const q=h?"?since="+encodeURIComponent(new Date(Date.now()-h*3600e3).toISOString().slice(0,19)):"";
  es=new EventSource("/events"+q);const dot=document.getElementById("dot");
  es.onopen=()=>dot.classList.add("live");es.onerror=()=>dot.classList.remove("live");
  es.onmessage=e=>render(JSON.parse(e.data));}
async function hook(post){
  try{const r=await fetch("/api/hook",post?{method:"POST",headers:{"X-Usage-Monitor":"1"}}:{});
  const s=(await r.json()).status;const bar=document.getElementById("hookbar");
  if(s==="connected"){bar.hidden=true;if(post)alertOk();return;}
  bar.hidden=false;document.getElementById("hooktext").textContent=s==="other"
   ?"Claude Code is set up with an older copy of the hook. Update it to use this app."
   :"Claude Code isn't sending usage here yet.";
  document.getElementById("hookbtn").textContent=s==="other"?"Update hook":"Connect Claude Code";}catch(e){}}
function alertOk(){const b=document.getElementById("hookbar");b.hidden=false;
  document.getElementById("hookbtn").hidden=true;
  document.getElementById("hooktext").textContent="Connected. New Claude Code sessions will report usage here.";
  setTimeout(()=>{b.hidden=true},6000);}
document.getElementById("hookbtn").onclick=()=>hook(true);hook(false);
document.getElementById("refresh").onclick=async()=>{
  const b=document.getElementById("refresh");b.disabled=true;b.textContent="Refreshing…";let msg="↻ Refresh";
  try{const r=await fetch("/api/refresh",{method:"POST",headers:{"X-Usage-Monitor":"1"}});const j=await r.json();
    if(r.ok){msg=j.imported?`↻ Refreshed (+${j.imported})`:"↻ Up to date";}else{msg="↻ Refresh failed";}}catch(e){msg="↻ Refresh failed";}
  connect();hook(false);b.textContent=msg;setTimeout(()=>{b.textContent="↻ Refresh";b.disabled=false;},2500);};
document.getElementById("range").onchange=connect;connect();
setInterval(connect,5*60e3); // refresh the time window now and then
</script></body></html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def make_server(host, port, db, hook_command=None):
    Handler.store = Store(db)
    Handler.hook_command = hook_command or default_hook_command()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local real-time Claude usage monitor")
    ap.add_argument("--host", default=os.environ.get("USAGE_MONITOR_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("USAGE_MONITOR_PORT", DEFAULT_PORT)))
    ap.add_argument("--db", default=os.environ.get("USAGE_MONITOR_DB", DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("send", help="post one usage event to a running monitor")
    s.add_argument("--model", required=True)
    s.add_argument("--input", type=int, default=0)
    s.add_argument("--output", type=int, default=0)
    s.add_argument("--cache-write", type=int, default=0)
    s.add_argument("--cache-read", type=int, default=0)
    s.add_argument("--source", default="manual")
    sub.add_parser("demo", help="post some sample events to a running monitor")
    args = ap.parse_args(argv)
    url = f"http://{args.host}:{args.port}/usage"

    if args.cmd == "send":
        print(post(url, {"model": args.model, "source": args.source, "usage": {
            "input_tokens": args.input, "output_tokens": args.output,
            "cache_creation_input_tokens": args.cache_write,
            "cache_read_input_tokens": args.cache_read}}))
        return
    if args.cmd == "demo":
        import random
        models = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
        for _ in range(12):
            print(post(url, {"model": random.choice(models), "source": "demo", "usage": {
                "input_tokens": random.randint(200, 6000), "output_tokens": random.randint(50, 2500),
                "cache_creation_input_tokens": random.choice([0, 0, 3000]),
                "cache_read_input_tokens": random.choice([0, 8000, 20000])}}))
            time.sleep(0.4)
        return

    httpd = make_server(args.host, args.port, args.db)
    print(f"Claude Usage Monitor running at http://{args.host}:{args.port}")
    print(f"  POST usage to  http://{args.host}:{args.port}/usage")
    print(f"  SQLite log at  {args.db}")
    print("  Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
