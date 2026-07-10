#!/usr/bin/env python3
"""
dashboard — the balancer's read-only view: "where did my prompt go?" (goal 12)
plus "what's the fleet doing right now?" (goal 13 — dashboard v2).

This is the visible face of goal 3's observability data. The gateway's
obs_callback (e2e/obs_callback.py) already emits a routing record per backend
ATTEMPT (event=llm_call) and per DELIVERED response (event=delivered). This
daemon is a SINK for those records plus a tiny read-only web UI that renders
them, so a human (or an agent) can literally look at where each prompt was
routed, whether it fell back, and how long it took.

FLEET VIEW (goal 13 — dashboard v2): the second half of the vision. The
control-plane (e2e/control_plane.py, goal 5) knows the live fleet state —
per model, across every workbench that has heartbeat: {warm, in_flight,
healthy, agent_capable}. This daemon READS that registry and renders it, so the
same page that shows where a prompt went also shows which workbenches are
subscribed, with which models, warm/healthy, and how loaded right now.

HOW THE FLEET DATA GETS HERE (reversible call, documented per CLAUDE.md + docs/10):
  SERVER-SIDE PROXY, not a browser-side fetch. `GET /api/fleet` reads the
  control-plane's `/models` from THIS process (urllib) and returns it; the page
  fetches only /api/fleet. Why:
    * ONE read surface. The registry->dashboard data path terminates in an
      endpoint WE own (/api/fleet), so the goal-13 assertion is deterministic —
      the same reason /api/records is owned, not scraped from LiteLLM's SPA.
    * No CORS, no second exposed port in the browser. The control-plane stays an
      internal-network daemon; only the dashboard is opened.
    * Graceful degrade. If CONTROL_PLANE_URL is unset or the control-plane is
      unreachable, /api/fleet returns {"available": false, ...} (HTTP 200) and
      the page shows "fleet unavailable" — it never 500s or hangs the viewer.
      That keeps a control-plane-less stack (bare pytest, the cli-auth profile)
      working exactly as before.
    * Reversible. Nothing here decides routing (that's Needs-a-human, docs/10);
      it only DISPLAYS state. Adopting a richer UI later forecloses nothing.

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
                                             #  "attempts":[...], "keys":[...],
                                             #  "overhead":{...}, "count":N} — the
                                             #  DATA ENDPOINT the UI fetches and
                                             #  the e2e test asserts.
                                             #  `requests` rows carry the caller's
                                             #  {key_alias,user_id,team_id} plus
                                             #  {tokens_delivered,tokens_consumed}
                                             #  (goal 20), `keys` is the per-key
                                             #  rollup (requests, fallbacks,
                                             #  tokens, cost), `overhead` is
                                             #  the goal-20 delivered-vs-consumed
                                             #  summary, and `policy_agreement`
                                             #  is the goal-24 shadow-policy
                                             #  chosen-vs-actual rollup
  GET  /api/fleet                            # {"available":bool, "models":[...],
                                             #  "instances":[...]} — the goal-13
                                             #  fleet data endpoint: a server-side
                                             #  read of the control-plane registry
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The control-plane registry (goal 5) this dashboard reads its fleet view from.
# Unset (default) => the fleet view degrades to "unavailable" and the rest of
# the dashboard (routing records) works exactly as before. Set in the dev + e2e
# compose files to the in-network control-plane address.
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "").rstrip("/")
# Kept short so a slow/hung control-plane can never wedge the viewer: /api/fleet
# fails fast to "unavailable" rather than blocking the page's poll.
_FLEET_TIMEOUT_S = 2.0


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


def _attempts_by_correlation(records: list) -> dict:
    """Index every `llm_call` attempt by its `correlation_id` (goal 16), in
    stream (chronological) order. This is the join table: a `delivered` request's
    correlation_id keys straight into its own attempt trail — the failed primary
    AND the winner of a fallback, which share the id (see obs_callback / docs/09).
    Attempts with no correlation_id are skipped here (they still show in the flat
    trail)."""
    idx: dict = {}
    for r in records:
        if r.get("event") != "llm_call":
            continue
        cid = r.get("correlation_id")
        if cid is None:
            continue
        idx.setdefault(cid, []).append(dict(r))
    return idx


def _consumed_tokens(attempts: list, delivered_total) -> int:
    """Total tokens a request ACTUALLY burned across every backend attempt
    (goal 20 — overhead attribution, the Fugu lesson: visible tokens are not
    consumed tokens once retries and fallbacks pile up).

    Sums `tokens.total` over the request's joined attempt trail — the failed
    primary, every retry, and the winner. Convention (documented in docs/09):
    an attempt reporting no usage counts 0; on the pinned litellm v1.83.14 that
    is every FAILED attempt (verified: failure events carry 0/0/0), so the
    gateway-visible consumed total is a LOWER BOUND on true backend burn.

    Winner inference: when the trail contains NO success attempt (the verified
    fallback-winner quirk — its success event may not fire, e.g. streamed
    winners), the delivered tokens stand in for the winner so it is never
    DROPPED; when a success attempt IS present its tokens are already in the
    sum, so delivered tokens are NOT added again — never double-counted."""
    consumed = 0
    has_success = False
    for a in attempts:
        t = (a.get("tokens") or {}).get("total")
        if isinstance(t, (int, float)):
            consumed += t
        if a.get("status") == "success":
            has_success = True
    if not has_success and isinstance(delivered_total, (int, float)):
        consumed += delivered_total
    return consumed


def _requests_view(records: list) -> list:
    """Fold the raw record stream into a per-REQUEST view — the primary
    "where did my prompt go?" table.

    A `delivered` record IS one client request's outcome: it names the alias the
    client asked for vs the backend that actually served it, the fallback flag,
    and the delivered tokens/cost. Newest-first.

    TRACE CORRELATION (goal 16): each row now carries its `correlation_id` and its
    NESTED `attempts` — the `llm_call` records that share that id, joined by it
    rather than by stream proximity. For a fallback that is the failed primary
    (the 503 "why") plus the winner; for a direct request it is the single
    successful attempt. This makes "why did THIS request fall back" answerable by
    nesting the attempt trail UNDER the request, not just alongside it. Requests
    with no correlation_id (older records, or a stack without the pre-call stamp)
    degrade to an empty `attempts` list and still stand on the delivered record.

    OVERHEAD ATTRIBUTION (goal 20 — the Fugu lesson): each row also carries
    `tokens_delivered` (what the client got) vs `tokens_consumed` (what ALL the
    request's backend attempts burned, summed over the joined trail — failed +
    retried + winner; attempts reporting no usage count 0). The winner is never
    dropped or double-counted: when the trail contains a success attempt its
    tokens are already in the sum; when it does not (the verified quirk — the
    fallback winner's success event may not fire), the delivered tokens stand in
    for the winner. See _consumed_tokens + docs/09."""
    by_cid = _attempts_by_correlation(records)
    out = []
    for r in records:
        if r.get("event") != "delivered":
            continue
        tokens = r.get("tokens") or {}
        cid = r.get("correlation_id")
        attempts = by_cid.get(cid, []) if cid is not None else []
        delivered_tokens = tokens.get("total")
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
                # OVERHEAD ATTRIBUTION (goal 20): what the client GOT vs what
                # the request's whole attempt trail BURNED. tokens_delivered
                # aliases tokens_total (kept for existing consumers); consumed
                # is the joined-trail sum (see _consumed_tokens).
                "tokens_delivered": delivered_tokens,
                "tokens_consumed": _consumed_tokens(attempts, delivered_tokens),
                # WHO asked (goal 15): the caller's synthetic identity, sourced
                # from obs_callback's read of UserAPIKeyAuth. Null under the
                # master key / no key store.
                "key_alias": r.get("key_alias"),
                "user_id": r.get("user_id"),
                "team_id": r.get("team_id"),
                # SHADOW complexity (goal 21): the deterministic classification
                # obs_callback stamped from request features — pure telemetry
                # for the future router, zero routing influence. None on records
                # from a stack without the tag.
                "complexity": r.get("complexity"),
                # SHADOW session classification (goal 22): session-turn vs
                # one-shot + the stickiness key a sticky router would pin on.
                # Same shadow discipline; None on untagged records.
                "session": r.get("session"),
                # SHADOW routing policy (goal 24): what the stateless
                # cheapest-capable arm WOULD have chosen vs what actually
                # happened — {arm, candidate_set, chosen, reason, registry,
                # actual, agree}. None on records from a stack without it.
                "policy": r.get("shadow_policy"),
                # TRACE CORRELATION (goal 16): the join key + this request's own
                # attempt trail, nested under it by that id.
                "correlation_id": cid,
                "attempts": attempts,
            }
        )
    out.reverse()  # newest first
    return out


# The label a per-key rollup uses when a delivered record carries no key_alias
# (master key / no key store — see obs_callback._identity). Kept out of the
# alias namespace so a real synthetic alias can never collide with it.
_NO_KEY = "(master key / no key)"


def _key_rollup(records: list) -> list:
    """Fold the delivered stream into a per-KEY rollup — "who asked, and what did
    it cost?" (goal 15). One row per distinct key_alias (records with no alias —
    master key / no key store — collapse into a single `(master key / no key)`
    row), carrying that key's request count, how many fell back, total tokens,
    and total cost. This is the identity counterpart to the per-request table:
    the request table shows individual prompts, this shows the spend/traffic
    shape per virtual key. Sorted by request volume (busiest first) for a stable,
    assertable order."""
    agg = {}
    order = []
    for r in records:
        if r.get("event") != "delivered":
            continue
        alias = r.get("key_alias")
        label = alias if alias else _NO_KEY
        entry = agg.get(label)
        if entry is None:
            entry = {
                "key_alias": alias,
                "user_id": r.get("user_id"),
                "team_id": r.get("team_id"),
                "requests": 0,
                "fallbacks": 0,
                "tokens": 0,
                "cost": 0.0,
            }
            agg[label] = entry
            order.append(label)
        entry["requests"] += 1
        if r.get("fallback"):
            entry["fallbacks"] += 1
        total = (r.get("tokens") or {}).get("total")
        if isinstance(total, (int, float)):
            entry["tokens"] += total
        cost = r.get("response_cost")
        if isinstance(cost, (int, float)):
            entry["cost"] += cost
    rows = [agg[k] for k in order]
    # Busiest key first; ties broken by label for determinism.
    rows.sort(key=lambda e: (-e["requests"], e["key_alias"] or _NO_KEY))
    return rows


def _overhead_rollup(records: list, requests: list) -> dict:
    """The at-a-glance overhead summary (goal 20): across every attributable
    request, how many tokens the clients were HANDED vs how many the backends
    BURNED — so a silently-expensive routing config (retry storms, flappy
    primaries forcing constant fallbacks) shows up as a ratio, not a vibe.
    This is the anti-Fugu instrument: Sakana's Fugu Ultra was reverse-engineered
    delivering ~2.2k visible tokens for ~22.7k consumed (10x, invisible to the
    client); this rollup exists so OUR gateway can never hide that shape.

    `unattributed_attempt_tokens` counts llm_call tokens whose correlation_id
    matches NO delivered record — on the pinned litellm that is chiefly STREAMED
    traffic (no delivered record fires for streamed responses; docs/09 caveat)
    plus requests that errored out entirely. Surfaced separately rather than
    folded into the ratio, so the per-request math stays exact and the gap
    stays VISIBLE instead of silently skewing the ratio."""
    delivered = 0
    consumed = 0
    for rq in requests:
        d = rq.get("tokens_delivered")
        if isinstance(d, (int, float)):
            delivered += d
        c = rq.get("tokens_consumed")
        if isinstance(c, (int, float)):
            consumed += c
    delivered_cids = {
        r.get("correlation_id")
        for r in records
        if r.get("event") == "delivered" and r.get("correlation_id") is not None
    }
    unattributed = 0
    for r in records:
        if r.get("event") != "llm_call":
            continue
        if r.get("correlation_id") in delivered_cids:
            continue
        t = (r.get("tokens") or {}).get("total")
        if isinstance(t, (int, float)):
            unattributed += t
    return {
        "requests": len(requests),
        "tokens_delivered": delivered,
        "tokens_consumed": consumed,
        "overhead_tokens": consumed - delivered,
        # consumed per delivered token; None when nothing delivered (no signal).
        "overhead_ratio": round(consumed / delivered, 3) if delivered else None,
        "unattributed_attempt_tokens": unattributed,
    }


def _complexity_buckets(records: list) -> dict:
    """The complexity DISTRIBUTION (goal 21): how the traffic mix splits across
    the shadow buckets — the request-shape telemetry the parked
    routing-granularity decision will be designed against. Counts `delivered`
    records by their complexity bucket; records without the tag (older stacks,
    unreadable messages) count under "unclassified" so the denominator stays
    honest — a mostly-unclassified distribution says "don't trust me yet"
    instead of quietly showing only the classifiable slice."""
    buckets: dict = {}
    for r in records:
        if r.get("event") != "delivered":
            continue
        cx = r.get("complexity") or {}
        bucket = cx.get("bucket") if isinstance(cx, dict) else None
        label = bucket if bucket else "unclassified"
        buckets[label] = buckets.get(label, 0) + 1
    return buckets


def _request_class_distribution(records: list) -> dict:
    """The request-class mix (goal 22): how much of the traffic is stateful
    conversation (routes STICKY under the decided hybrid granularity) vs
    stateless one-shots (routes freely) — the load-shape number the hybrid
    router's capacity planning hangs on. Counts `delivered` records by
    session.request_class; untagged records count `unclassified` (same honesty
    convention as the complexity buckets)."""
    dist: dict = {}
    for r in records:
        if r.get("event") != "delivered":
            continue
        sess = r.get("session") or {}
        cls = sess.get("request_class") if isinstance(sess, dict) else None
        label = cls if cls else "unclassified"
        dist[label] = dist.get(label, 0) + 1
    return dist


def _policy_agreement(records: list) -> dict:
    """The chosen-vs-actual AGREEMENT rollup (goal 24): across every delivered
    request the shadow policy evaluated, how often would the stateless
    cheapest-capable arm have routed to the backend that actually served? This
    is the number the enforcement flip (goal 26) is judged against — a policy
    that disagrees with reality constantly is either finding real savings or
    misconfigured, and either way you want to SEE it before it drives routing.
    `unevaluated` counts delivered records with no verdict (no policy block —
    older stacks — or agree:null, e.g. no surviving candidate), keeping the
    denominator honest like the complexity/class rollups."""
    agree = 0
    disagree = 0
    unevaluated = 0
    for r in records:
        if r.get("event") != "delivered":
            continue
        pol = r.get("shadow_policy")
        verdict = pol.get("agree") if isinstance(pol, dict) else None
        if verdict is True:
            agree += 1
        elif verdict is False:
            disagree += 1
        else:
            unevaluated += 1
    evaluated = agree + disagree
    return {
        "evaluated": evaluated,
        "agree": agree,
        "disagree": disagree,
        "unevaluated": unevaluated,
        # Share of evaluated requests where policy and reality matched; None
        # when nothing was evaluated (no signal, not a fake 100%).
        "agreement_rate": round(agree / evaluated, 3) if evaluated else None,
    }


def _attempts_view(records: list) -> list:
    """The per-ATTEMPT trail (event=llm_call): every backend tried, its tier,
    latency, tokens, and — on failure — the error that triggered a fallback.
    Newest first."""
    out = [dict(r) for r in records if r.get("event") == "llm_call"]
    out.reverse()
    return out


# --- the fleet view (goal 13) -----------------------------------------------
# A server-side read of the control-plane registry (goal 5). See the module
# docstring for WHY this is a proxy and not a browser-side fetch.


def _fetch_fleet() -> dict:
    """Read the control-plane's per-model aggregate (`GET /models`) and reshape
    it into the dashboard's fleet payload.

    The control-plane's /models already embeds each model's per-instance rows
    (its `instances` drill-down), each carrying the DERIVED health + staleness —
    so a silent workbench shows up here as unhealthy without this daemon knowing
    anything about TTLs. We surface two views:
      * models     — the per-model aggregate (warm, healthy/total, in_flight,
                     agent_capable) — the headline "what can the fleet do".
      * instances  — a flat per-(workbench,model) list folded out of the models'
                     drill-downs — the "which box is subscribed, is it healthy,
                     how loaded" table.

    Returns {"available": True, "control_plane_url", "models", "instances"} on a
    clean read, or {"available": False, "error"} when the control-plane is
    unconfigured / unreachable / speaking gibberish. NEVER raises — the caller
    serves this at HTTP 200 so the page degrades to "fleet unavailable" instead
    of erroring."""
    if not CONTROL_PLANE_URL:
        return {"available": False, "error": "CONTROL_PLANE_URL not configured"}
    try:
        req = urllib.request.Request(
            CONTROL_PLANE_URL + "/models", headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=_FLEET_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — any failure => degrade, never 500
        return {
            "available": False,
            "control_plane_url": CONTROL_PLANE_URL,
            "error": "control-plane unreachable: %s" % e,
        }
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return {
            "available": False,
            "control_plane_url": CONTROL_PLANE_URL,
            "error": "control-plane /models returned an unexpected shape",
        }
    instances = []
    for m in models:
        for inst in m.get("instances") or []:
            instances.append(inst)
    # Stable order for a deterministic table + assertion: workbench, then model.
    instances.sort(key=lambda i: (i.get("workbench_id") or "", i.get("model") or ""))
    return {
        "available": True,
        "control_plane_url": CONTROL_PLANE_URL,
        "models": models,
        "instances": instances,
    }


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
  .badge.healthy { color:var(--ok); border:1px solid var(--ok); }
  .badge.unhealthy { color:var(--warn); border:1px solid var(--warn); }
  .badge.stale { color:var(--fall); border:1px solid var(--fall); }
  .badge.yes { color:var(--accent); border:1px solid var(--accent); }
  .badge.no { color:var(--muted); border:1px solid var(--muted); }
  .fleetstatus { text-transform:none; letter-spacing:0; font-weight:400;
    color:var(--warn); margin-left:8px; }
  .num { text-align:right; font-variant-numeric:tabular-nums; }
  .muted { color:var(--muted); }
  .empty { color:var(--muted); padding:18px 12px; }
  code { color:var(--fg); }
  /* trace correlation (goal 16): the attempt trail nested UNDER its request */
  tr.trail td { border-bottom:1px solid var(--line); padding:4px 12px 8px;
    white-space:normal; color:var(--muted); font-size:12px; }
  tr.trail .cid { color:var(--muted); }
  .attempt { display:inline-block; padding:1px 6px; margin:2px 2px;
    border:1px solid var(--line); border-radius:6px; }
  .attempt.failed { border-color:var(--warn); }
  .attempt.ok { border-color:var(--ok); }
</style>
</head>
<body>
<header>
  <h1>Router dashboard</h1>
  <span class="sub">where did my prompt go? &amp; what's the fleet doing? &middot; goals 12 + 13</span>
  <span class="status" id="status">loading&hellip;</span>
</header>
<div class="wrap">
  <h2>Fleet &mdash; workbenches subscribed, models carried, health &amp; load
    <span class="fleetstatus" id="fleetstatus"></span></h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>model</th><th>health</th><th class="num">warm</th>
        <th class="num">healthy</th><th class="num">in&#8209;flight</th><th>agent</th>
      </tr></thead>
      <tbody id="fleetmodels"><tr><td class="empty" colspan="6">no fleet data</td></tr></tbody>
    </table>
  </div>
  <div class="tablewrap" style="margin-top:10px">
    <table>
      <thead><tr>
        <th>workbench</th><th>model</th><th>health</th><th class="num">warm</th>
        <th class="num">in&#8209;flight</th><th class="num">age</th>
      </tr></thead>
      <tbody id="fleetinstances"><tr><td class="empty" colspan="6">no workbenches subscribed</td></tr></tbody>
    </table>
  </div>

  <h2>Per key &mdash; who asked, and what it cost</h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>key</th><th>user</th><th>team</th><th class="num">requests</th>
        <th class="num">fallbacks</th><th class="num">tokens</th><th class="num">cost</th>
      </tr></thead>
      <tbody id="keys"><tr><td class="empty" colspan="7">no keyed traffic yet</td></tr></tbody>
    </table>
  </div>

  <h2>Requests &mdash; requested alias &rarr; backend that served it
    <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">&middot; each row nests its own attempt trail (goal 16)</span>
    <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400" id="overhead"></span>
    <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400" id="cxdist"></span></h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>requested</th><th></th><th>served</th><th>route</th><th>policy</th><th>complexity</th><th>class</th>
        <th>key</th><th>user</th>
        <th>provider</th><th class="num">delivered</th><th class="num">consumed</th><th class="num">cost</th>
      </tr></thead>
      <tbody id="requests"><tr><td class="empty" colspan="13">no requests yet</td></tr></tbody>
    </table>
  </div>

  <h2>Attempt trail &mdash; every backend tried, and why a fallback fired
    <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">&middot; ttft = time-to-first-token, streamed only (goal 18)</span></h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>group</th><th>backend</th><th>tier</th><th>status</th>
        <th class="num">ttft</th><th class="num">latency</th><th class="num">tokens</th><th>error</th>
      </tr></thead>
      <tbody id="attempts"><tr><td class="empty" colspan="8">no attempts yet</td></tr></tbody>
    </table>
  </div>
</div>

<script>
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function idCell(v){
  // Synthetic identity, or a muted dash when null (master key / no key store).
  return v==null ? '<span class="muted">&mdash;</span>' : '<code>'+esc(v)+'</code>';
}
function attemptChip(a){
  // One backend attempt in a request's nested trail. A failure shows the error
  // code that triggered the fallback (the "why"); a success shows its tier.
  const failed = a.status==='failure';
  const tier = a.tier ? ' <span class="badge tier">'+esc(a.tier)+'</span>' : '';
  // ttft (streamed only, goal 18) shown alongside completion latency: "12ms ttft / 40ms".
  const lat = a.latency_ms==null ? ''
    : ' <span class="muted">'+(a.ttft_ms==null?'':esc(a.ttft_ms)+'ms ttft / ')+esc(a.latency_ms)+'ms</span>';
  const mark = failed
    ? ' <span class="badge fail">'+esc(a.error_code||'fail')+'</span>'
    : ' <span class="badge direct">ok</span>';
  return '<span class="attempt '+(failed?'failed':'ok')+'"><code>'
    + esc(a.backend||a.requested_group)+'</code>'+tier+mark+lat+'</span>';
}
function reqTrail(r){
  // The attempt trail joined to THIS request by correlation_id (goal 16),
  // rendered as a sub-row nested under the request. Empty when a request has no
  // correlated attempts (older records / a stack without the pre-call stamp).
  const atts = r.attempts||[];
  if(!atts.length) return '';
  const chips = atts.map(attemptChip).join('<span class="arrow"> &rarr; </span>');
  const cid = r.correlation_id ? ' <span class="cid">('+esc(r.correlation_id)+')</span>' : '';
  return '<tr class="trail"><td></td><td colspan="12">why'+cid+': '+chips+'</td></tr>';
}
// Shadow routing policy (goal 24): would the stateless cheapest-capable arm
// have routed here? agree/disagree as a badge, the chosen backend + the full
// citable reason (and registry degrade mode) on hover — auditable in place.
function polCell(p){
  if(!p) return '<span class="muted">&mdash;</span>';
  const why = 'policy chose '+(p.chosen||'(none)')+' \\u00b7 registry '+(p.registry||'?')
    +' \\u00b7 '+(p.reason||'');
  if(p.agree===true)
    return '<span class="badge direct" title="'+esc(why)+'">agree</span>';
  if(p.agree===false)
    return '<span class="badge fall" title="'+esc(why)+'">chose '+esc(p.chosen)+'</span>';
  return '<span class="badge no" title="'+esc(why)+'">no verdict</span>';
}
// Shadow session classification (goal 22): session-turn vs one-shot, with the
// stickiness key + its source on hover — the hybrid-granularity telemetry.
function sessCell(s){
  if(!s || !s.request_class) return '<span class="muted">&mdash;</span>';
  const isSession = s.request_class==='session-turn';
  const cls = isSession ? 'yes' : 'no';
  const why = s.stickiness_key
    ? ('key '+s.stickiness_key+' ('+(s.key_source||'?')+')')
    : 'no stickiness key';
  return '<span class="badge '+cls+'" title="'+esc(why)+'">'+esc(s.request_class)+'</span>';
}
// Shadow complexity (goal 21): the bucket as a badge, the full feature vector
// as a hover title — every classification auditable in place, never a mystery.
const CX_CLASS = {trivial:'no', toolful:'tier', heavy:'fail', agentic:'fall'};
function cxCell(cx){
  if(!cx || !cx.bucket) return '<span class="muted">&mdash;</span>';
  const cls = CX_CLASS[cx.bucket] || 'no';
  const why = '~'+cx.approx_prompt_tokens+' tok / '+cx.turns+' turns / '+cx.tools+' tools';
  return '<span class="badge '+cls+'" title="'+esc(why)+'">'+esc(cx.bucket)+'</span>';
}
function reqRow(r){
  const badge = r.fallback
    ? '<span class="badge fall">fallback</span>'
    : '<span class="badge direct">direct</span>';
  const del = r.tokens_delivered==null ? '&mdash;' : esc(r.tokens_delivered);
  // consumed > delivered means backend burn the client never saw (goal 20) —
  // flag it, don't bury it in a same-looking number.
  const over = r.tokens_consumed!=null && r.tokens_consumed > (r.tokens_delivered||0);
  const con = r.tokens_consumed==null ? '&mdash;'
    : (over ? '<span class="badge fall">'+esc(r.tokens_consumed)+'</span>' : esc(r.tokens_consumed));
  const cost = r.response_cost==null ? '&mdash;' : ('$'+Number(r.response_cost).toFixed(6));
  return '<tr>'
    + '<td><code>'+esc(r.requested_model)+'</code></td>'
    + '<td class="arrow">&rarr;</td>'
    + '<td><code>'+esc(r.served_model)+'</code></td>'
    + '<td>'+badge+'</td>'
    + '<td>'+polCell(r.policy)+'</td>'
    + '<td>'+cxCell(r.complexity)+'</td>'
    + '<td>'+sessCell(r.session)+'</td>'
    + '<td>'+idCell(r.key_alias)+'</td>'
    + '<td>'+idCell(r.user_id)+'</td>'
    + '<td>'+esc(r.provider)+'</td>'
    + '<td class="num">'+del+'</td>'
    + '<td class="num">'+con+'</td>'
    + '<td class="num">'+cost+'</td>'
    + '</tr>'
    + reqTrail(r);  // goal 16: nest this request's attempt trail beneath it
}
function keyRow(k){
  const alias = k.key_alias==null
    ? '<span class="muted">master key / no key</span>'
    : '<code>'+esc(k.key_alias)+'</code>';
  const fb = k.fallbacks
    ? '<span class="badge fall">'+esc(k.fallbacks)+'</span>' : '0';
  const cost = (k.cost==null) ? '&mdash;' : ('$'+Number(k.cost).toFixed(6));
  return '<tr>'
    + '<td>'+alias+'</td>'
    + '<td>'+idCell(k.user_id)+'</td>'
    + '<td>'+idCell(k.team_id)+'</td>'
    + '<td class="num">'+esc(k.requests)+'</td>'
    + '<td class="num">'+fb+'</td>'
    + '<td class="num">'+esc(k.tokens)+'</td>'
    + '<td class="num">'+cost+'</td>'
    + '</tr>';
}
function attRow(a){
  const t = a.tier ? '<span class="badge tier">'+esc(a.tier)+'</span>' : '&mdash;';
  const failed = a.status==='failure';
  const st = failed ? '<span class="badge fail">failure</span>' : esc(a.status);
  // ttft is present only on streamed attempts (goal 18); a dash for non-streamed.
  const ttft = a.ttft_ms==null ? '&mdash;' : (esc(a.ttft_ms)+' ms');
  const lat = a.latency_ms==null ? '&mdash;' : (esc(a.latency_ms)+' ms');
  const tok = (a.tokens&&a.tokens.total!=null) ? esc(a.tokens.total) : '&mdash;';
  const err = a.error_code ? ('<code>'+esc(a.error_code)+' '+esc(a.error_class||'')+'</code>') : '&mdash;';
  return '<tr>'
    + '<td><code>'+esc(a.requested_group)+'</code></td>'
    + '<td><code>'+esc(a.backend)+'</code></td>'
    + '<td>'+t+'</td>'
    + '<td>'+st+'</td>'
    + '<td class="num">'+ttft+'</td>'
    + '<td class="num">'+lat+'</td>'
    + '<td class="num">'+tok+'</td>'
    + '<td>'+err+'</td>'
    + '</tr>';
}
function healthBadge(inst){
  if(inst.stale) return '<span class="badge stale">stale</span>';
  return inst.healthy
    ? '<span class="badge healthy">healthy</span>'
    : '<span class="badge unhealthy">unhealthy</span>';
}
function fleetModelRow(m){
  const anyStale = (m.instances||[]).some(i=>i.stale);
  const health = m.healthy>0
    ? '<span class="badge healthy">healthy</span>'
    : '<span class="badge unhealthy">down</span>';
  const agent = m.agent_capable
    ? '<span class="badge yes">yes</span>' : '<span class="badge no">no</span>';
  return '<tr>'
    + '<td><code>'+esc(m.model)+'</code></td>'
    + '<td>'+health+(anyStale?' <span class="badge stale">stale</span>':'')+'</td>'
    + '<td class="num">'+esc(m.warm)+'</td>'
    + '<td class="num">'+esc(m.healthy)+'/'+esc(m.instances_total)+'</td>'
    + '<td class="num">'+esc(m.in_flight)+'</td>'
    + '<td>'+agent+'</td>'
    + '</tr>';
}
function fleetInstRow(i){
  const warm = i.warm ? '<span class="badge yes">warm</span>' : '<span class="badge no">cold</span>';
  const age = i.age_ms==null ? '&mdash;' : (Math.round(i.age_ms/100)/10+' s');
  return '<tr>'
    + '<td><code>'+esc(i.workbench_id)+'</code></td>'
    + '<td><code>'+esc(i.model)+'</code></td>'
    + '<td>'+healthBadge(i)+'</td>'
    + '<td class="num">'+warm+'</td>'
    + '<td class="num">'+esc(i.in_flight)+'</td>'
    + '<td class="num">'+age+'</td>'
    + '</tr>';
}
async function refreshFleet(){
  const fs = document.getElementById('fleetstatus');
  try {
    const res = await fetch('api/fleet', {cache:'no-store'});
    const data = await res.json();
    if(!data.available){
      fs.textContent = '\\u2014 fleet unavailable ('+esc(data.error||'no control-plane')+')';
      document.getElementById('fleetmodels').innerHTML =
        '<tr><td class="empty" colspan="6">control-plane not reachable</td></tr>';
      document.getElementById('fleetinstances').innerHTML =
        '<tr><td class="empty" colspan="6">&mdash;</td></tr>';
      return;
    }
    const models = data.models||[], insts = data.instances||[];
    fs.textContent = '';
    document.getElementById('fleetmodels').innerHTML = models.length
      ? models.map(fleetModelRow).join('')
      : '<tr><td class="empty" colspan="6">no models registered</td></tr>';
    document.getElementById('fleetinstances').innerHTML = insts.length
      ? insts.map(fleetInstRow).join('')
      : '<tr><td class="empty" colspan="6">no workbenches subscribed</td></tr>';
  } catch(e) {
    fs.textContent = '\\u2014 fleet endpoint error';
  }
}
async function refresh(){
  try {
    const res = await fetch('api/records', {cache:'no-store'});
    const data = await res.json();
    const reqs = data.requests||[], atts = data.attempts||[], keys = data.keys||[];
    document.getElementById('keys').innerHTML = keys.length
      ? keys.map(keyRow).join('')
      : '<tr><td class="empty" colspan="7">no keyed traffic yet</td></tr>';
    document.getElementById('requests').innerHTML = reqs.length
      ? reqs.map(reqRow).join('')
      : '<tr><td class="empty" colspan="13">no requests yet</td></tr>';
    document.getElementById('attempts').innerHTML = atts.length
      ? atts.map(attRow).join('')
      : '<tr><td class="empty" colspan="8">no attempts yet</td></tr>';
    // goal 20: the delivered-vs-consumed rollup, at a glance (the Fugu lesson).
    const ov = data.overhead;
    document.getElementById('overhead').innerHTML = !ov ? ''
      : '&middot; &Sigma; delivered '+esc(ov.tokens_delivered)
        +' / consumed '+esc(ov.tokens_consumed)
        +(ov.overhead_ratio!=null ? ' (ratio '+esc(ov.overhead_ratio)+')' : '')
        +(ov.unattributed_attempt_tokens ? ' &middot; +'+esc(ov.unattributed_attempt_tokens)+' unattributed (streamed/aborted)' : '');
    // goal 21: the shadow-complexity traffic mix, at a glance.
    const cx = data.complexity_buckets || {};
    const mix = Object.keys(cx).sort().map(k=>esc(k)+' '+esc(cx[k])).join(' / ');
    // goal 22: the session-turn vs one-shot mix rides the same strip.
    const rc = data.request_classes || {};
    const cmix = Object.keys(rc).sort().map(k=>esc(k)+' '+esc(rc[k])).join(' / ');
    // goal 24: shadow-policy agreement at a glance — chosen-vs-actual across
    // every evaluated request (the number goal 26's enforcement flip is judged
    // against).
    const pa = data.policy_agreement;
    const pmix = (pa && pa.evaluated)
      ? ' &middot; policy: '+esc(pa.agree)+'/'+esc(pa.evaluated)+' agree'
        +(pa.agreement_rate!=null ? ' ('+esc(pa.agreement_rate)+')' : '')
        +(pa.unevaluated ? ', '+esc(pa.unevaluated)+' unevaluated' : '')
      : '';
    document.getElementById('cxdist').innerHTML =
      (mix ? '&middot; mix: '+mix : '') + (cmix ? ' &middot; class: '+cmix : '') + pmix;
    document.getElementById('status').textContent =
      reqs.length+' requests \\u00b7 '+atts.length+' attempts \\u00b7 '+(data.count||0)+' records';
  } catch(e) {
    document.getElementById('status').textContent = 'sink unreachable';
  }
}
function tick(){ refresh(); refreshFleet(); }
tick();
setInterval(tick, 2000);
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
            requests = _requests_view(recs)
            return self._json(
                200,
                {
                    "count": len(recs),
                    "requests": requests,
                    "attempts": _attempts_view(recs),
                    "keys": _key_rollup(recs),
                    # goal 20: delivered-vs-consumed at a glance (the Fugu 10x
                    # lesson) — see _overhead_rollup.
                    "overhead": _overhead_rollup(recs, requests),
                    # goal 21: the shadow-complexity traffic mix — see
                    # _complexity_buckets.
                    "complexity_buckets": _complexity_buckets(recs),
                    # goal 22: session-turn vs one-shot mix — see
                    # _request_class_distribution.
                    "request_classes": _request_class_distribution(recs),
                    # goal 24: shadow-policy chosen-vs-actual agreement — see
                    # _policy_agreement.
                    "policy_agreement": _policy_agreement(recs),
                    "records": recs,
                },
            )
        # Goal 13 — the fleet DATA ENDPOINT the UI fetches and the e2e assertion
        # covers: a server-side read of the control-plane registry. Always 200,
        # even when the control-plane is down (available:false in the body).
        if self.path.startswith("/api/fleet"):
            return self._json(200, _fetch_fleet())
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
