#!/usr/bin/env python3
"""
dashboard — the balancer's read-only "where did my prompt go?" view (goal 12).

This is the visible face of goal 3's observability data. The gateway's
obs_callback (e2e/obs_callback.py) already emits a routing record per backend
ATTEMPT (event=llm_call) and per DELIVERED response (event=delivered). This
daemon is a SINK for those records plus a tiny read-only web UI that renders
them, so a human (or an agent) can literally look at where each prompt was
routed, whether it fell back, and how long it took.

BUILD-vs-REUSE (reversible call, documented per CLAUDE.md + docs/09):
  We BUILD a thin read-only page rather than reuse LiteLLM's bundled admin UI.
  Why:
    * The data is OURS. The {requested alias, backend served, fallback?, tier,
      latency, tokens} shape is produced by obs_callback (goal 3), not LiteLLM.
      LiteLLM's UI renders its own SpendLogs/keys/teams and has no notion of our
      fallback "why" (the 503 that triggered it, the backend tier).
    * Machine-verifiable. Goal 12 requires "an e2e assertion covers the data
      endpoint feeding the dashboard." A JSON endpoint we own (/api/records) is
      deterministically assertable; LiteLLM's React SPA behind master-key auth
      is brittle to assert on.
    * Dependency-free floor. Matches the project ethos (stdlib mockd, no
      Langfuse/OTEL per docs/09) — a stdlib viewer keeps the "zero external
      observability stack" invariant.
    * Read-only, minimal auth surface. LiteLLM's UI is read-write (mint keys)
      and needs the master key in-browser. This view only reads.
    * Reversible. Records still flow to stdout + Postgres; adopting LiteLLM's
      UI or Grafana later forecloses nothing.

Stdlib only — no pip install, runs bare or in a slim container. Python 3.9+.

HTTP surface:
  POST /records        {<routing record>}   # obs_callback webhook target; append
  GET  /api/records                          # {"records":[...], "requests":[...],
                                             #  "count":N} — the DATA ENDPOINT the
                                             #  UI fetches and the e2e test asserts
  GET  /                                      # read-only HTML dashboard
  POST /__reset                               # clear all records (test isolation)
  GET  /health                                # 200 (liveness)

The sink is unauthenticated — like mockd, this is a TEST/DEV daemon; bind it to
localhost / an internal compose network only.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Records:
    """Thread-safe, in-memory routing-record store.

    The gateway's obs_callback POSTs one record per backend attempt / delivered
    response to /records; the UI reads them back via GET /api/records. A single
    process centralizes records across BOTH litellm workers (--num_workers 2),
    which an in-callback list could not. Cleared by /__reset so records never
    leak across serially-run e2e tests."""

    _CAP = 5000  # bound memory across a long-lived dev stack; oldest are dropped.

    def __init__(self):
        self._lock = threading.Lock()
        self._records = []

    def add(self, record: dict) -> None:
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._CAP:
                self._records = self._records[-self._CAP :]

    def all(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._records))

    def reset(self) -> None:
        with self._lock:
            self._records = []


RECORDS = Records()


def _requests_view(records: list) -> list:
    """Fold the raw record stream into a per-REQUEST view — the primary
    "where did my prompt go?" table.

    A `delivered` record IS one client request's outcome: it names the alias the
    client asked for vs the backend that actually served it, the fallback flag,
    and the delivered tokens/cost. We surface those newest-first and attach any
    `llm_call` FAILURE attempts that share a nearby position in the stream as the
    "why" — but we do NOT try to hard-correlate by id (delivered records carry no
    trace_id; see docs/09), so the attempt trail is offered separately and the
    per-request row stands on the delivered record alone."""
    out = []
    for r in records:
        if r.get("event") != "delivered":
            continue
        tokens = r.get("tokens") or {}
        out.append(
            {
                "requested_model": r.get("requested_model"),
                "served_model": r.get("served_model"),
                "served_model_id": r.get("served_model_id"),
                "provider": r.get("provider"),
                "api_base": r.get("api_base"),
                "fallback": bool(r.get("fallback")),
                "response_cost": r.get("response_cost"),
                "tokens_total": tokens.get("total"),
                "tokens_prompt": tokens.get("prompt"),
                "tokens_completion": tokens.get("completion"),
            }
        )
    out.reverse()  # newest first
    return out


def _attempts_view(records: list) -> list:
    """The per-ATTEMPT trail (event=llm_call): every backend tried, its tier,
    latency, tokens, and — on failure — the error that triggered a fallback.
    Newest first."""
    out = [dict(r) for r in records if r.get("event") == "llm_call"]
    out.reverse()
    return out


# --- the read-only page -----------------------------------------------------
# Inlined HTML/CSS/JS: served from a container with no outbound network, so no
# external assets. The page polls /api/records and re-renders; it never writes.

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Router dashboard — where did my prompt go?</title>
<style>
  :root {
    --bg:#0f1117; --panel:#171a23; --line:#262b38; --fg:#e6e9ef; --muted:#8b93a7;
    --ok:#3fb950; --warn:#d29922; --fall:#f0883e; --accent:#58a6ff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { padding:16px 20px; border-bottom:1px solid var(--line);
    display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  .sub { color:var(--muted); font-size:12px; }
  .status { margin-left:auto; color:var(--muted); font-size:12px; }
  .wrap { padding:20px; max-width:1200px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
    color:var(--muted); margin:26px 0 10px; }
  .tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px;
    background:var(--panel); }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th,td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line);
    white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0;
    background:var(--panel); }
  tr:last-child td { border-bottom:none; }
  .arrow { color:var(--muted); }
  .badge { display:inline-block; padding:1px 8px; border-radius:999px;
    font-size:11px; font-weight:600; }
  .badge.direct { color:var(--ok); border:1px solid var(--ok); }
  .badge.fall { color:var(--fall); border:1px solid var(--fall); }
  .badge.tier { color:var(--accent); border:1px solid var(--accent); }
  .badge.fail { color:var(--warn); border:1px solid var(--warn); }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .empty { color:var(--muted); padding:18px 12px; }
  code { color:var(--fg); }
</style>
</head>
<body>
<header>
  <h1>Router dashboard</h1>
  <span class="sub">where did my prompt go? &middot; goal 12 (read-only view over goal-3 routing records)</span>
  <span class="status" id="status">loading&hellip;</span>
</header>
<div class="wrap">
  <h2>Requests &mdash; requested alias &rarr; backend that served it</h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>requested</th><th></th><th>served</th><th>route</th>
        <th>provider</th><th class="num">tokens</th><th class="num">cost</th>
      </tr></thead>
      <tbody id="requests"><tr><td class="empty" colspan="7">no requests yet</td></tr></tbody>
    </table>
  </div>

  <h2>Attempt trail &mdash; every backend tried, and why a fallback fired</h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>group</th><th>backend</th><th>tier</th><th>status</th>
        <th class="num">latency</th><th class="num">tokens</th><th>error</th>
      </tr></thead>
      <tbody id="attempts"><tr><td class="empty" colspan="7">no attempts yet</td></tr></tbody>
    </table>
  </div>
</div>

<script>
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function reqRow(r){
  const badge = r.fallback
    ? '<span class="badge fall">fallback</span>'
    : '<span class="badge direct">direct</span>';
  const tok = r.tokens_total==null ? '&mdash;' : esc(r.tokens_total);
  const cost = r.response_cost==null ? '&mdash;' : ('$'+Number(r.response_cost).toFixed(6));
  return '<tr>'
    + '<td><code>'+esc(r.requested_model)+'</code></td>'
    + '<td class="arrow">&rarr;</td>'
    + '<td><code>'+esc(r.served_model)+'</code></td>'
    + '<td>'+badge+'</td>'
    + '<td>'+esc(r.provider)+'</td>'
    + '<td class="num">'+tok+'</td>'
    + '<td class="num">'+cost+'</td>'
    + '</tr>';
}
function attRow(a){
  const t = a.tier ? '<span class="badge tier">'+esc(a.tier)+'</span>' : '&mdash;';
  const failed = a.status==='failure';
  const st = failed ? '<span class="badge fail">failure</span>' : esc(a.status);
  const lat = a.latency_ms==null ? '&mdash;' : (esc(a.latency_ms)+' ms');
  const tok = (a.tokens&&a.tokens.total!=null) ? esc(a.tokens.total) : '&mdash;';
  const err = a.error_code ? ('<code>'+esc(a.error_code)+' '+esc(a.error_class||'')+'</code>') : '&mdash;';
  return '<tr>'
    + '<td><code>'+esc(a.requested_group)+'</code></td>'
    + '<td><code>'+esc(a.backend)+'</code></td>'
    + '<td>'+t+'</td>'
    + '<td>'+st+'</td>'
    + '<td class="num">'+lat+'</td>'
    + '<td class="num">'+tok+'</td>'
    + '<td>'+err+'</td>'
    + '</tr>';
}
async function refresh(){
  try {
    const res = await fetch('api/records', {cache:'no-store'});
    const data = await res.json();
    const reqs = data.requests||[], atts = data.attempts||[];
    document.getElementById('requests').innerHTML = reqs.length
      ? reqs.map(reqRow).join('')
      : '<tr><td class="empty" colspan="7">no requests yet</td></tr>';
    document.getElementById('attempts').innerHTML = atts.length
      ? atts.map(attRow).join('')
      : '<tr><td class="empty" colspan="7">no attempts yet</td></tr>';
    document.getElementById('status').textContent =
      reqs.length+' requests \\u00b7 '+atts.length+' attempts \\u00b7 '+(data.count||0)+' records';
  } catch(e) {
    document.getElementById('status').textContent = 'sink unreachable';
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[dashboard] " + (fmt % args) + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok", "daemon": "dashboard"})
        # The DATA ENDPOINT the UI fetches and the e2e assertion covers.
        if self.path.startswith("/api/records"):
            recs = RECORDS.all()
            return self._json(
                200,
                {
                    "count": len(recs),
                    "requests": _requests_view(recs),
                    "attempts": _attempts_view(recs),
                    "records": recs,
                },
            )
        if (
            self.path == "/"
            or self.path.startswith("/index")
            or self.path.startswith("/dashboard")
        ):
            return self._html(200, _PAGE)
        return self._json(404, {"error": "not found: " + self.path})

    def do_POST(self):
        if self.path.startswith("/records"):
            # The gateway's obs_callback publishes routing records here.
            RECORDS.add(self._read_body())
            return self._json(200, {"ok": True})
        if self.path.startswith("/__reset"):
            RECORDS.reset()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found: " + self.path})


def main():
    port = int(os.environ.get("DASH_PORT", "9300"))
    host = os.environ.get("DASH_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(
        "dashboard listening on http://%s:%d (GET / , GET /api/records , POST /records)"
        % (host, port)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
