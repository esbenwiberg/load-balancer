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
                                             #  summary, `policy_agreement`
                                             #  is the goal-24 shadow-policy
                                             #  chosen-vs-actual rollup (plus
                                             #  the goal-27 enforced split),
                                             #  and goal 27 adds `models`
                                             #  (traffic per model, demand vs
                                             #  supply), `users`, `sessions`
                                             #  (per stickiness key: turns,
                                             #  pin state, escalation) and
                                             #  `backends` (per deployment)
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
import time
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
            # SINK ARRIVAL TIME (goal 27). Routing records carry latency but no
            # wall-clock stamp of their own, so the sink stamps arrival — good
            # enough for "most recent activity" ordering (sessions view) and
            # honest about what it is: when the record REACHED the dashboard,
            # not when the backend answered. Never overwrites a stamp already
            # present (a future obs_callback may stamp at the source).
            record.setdefault("received_at", time.time())
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
                # STREAMED delivery (goal 29): true when this request's
                # delivered record was built from the post-stream success
                # event (obs_callback._delivered_stream_record). False on
                # non-streamed records, which omit the key.
                "stream": bool(r.get("stream")),
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
                # Sink arrival time (goal 27) — ordering signal for the
                # sessions rollup; None on records injected without one.
                "received_at": r.get("received_at"),
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


def _model_rollup(requests: list) -> list:
    """Per-MODEL traffic (goal 27): demand vs supply per alias. One row per
    model name seen in the delivered stream, carrying:
      * requested — how many requests ASKED for this alias (demand). Under
        enforcement/fallback this can differ wildly from served — that drift
        IS the router working (or misfiring), so both counts sit side by side.
      * served    — how many requests this model actually answered (supply),
        plus the tokens/cost attributed to serving them and how many of those
        wins arrived via fallback.
    Note: `requested` uses the record's requested_model, which under
    ROUTER_POLICY=enforce is the post-rewrite ask (the original ask lives in
    the policy block's `requested` stash) — so under enforcement this shows
    what the ROUTER asked the backend pool for. Sorted busiest-served first."""
    agg: dict = {}
    order = []

    def _entry(name):
        e = agg.get(name)
        if e is None:
            e = {
                "model": name,
                "requested": 0,
                "served": 0,
                "fallbacks_in": 0,
                "tokens_delivered": 0,
                "tokens_consumed": 0,
                "cost": 0.0,
            }
            agg[name] = e
            order.append(name)
        return e

    for rq in requests:
        req_name = rq.get("requested_model")
        srv_name = rq.get("served_model")
        if req_name:
            _entry(req_name)["requested"] += 1
        if srv_name:
            e = _entry(srv_name)
            e["served"] += 1
            if rq.get("fallback"):
                e["fallbacks_in"] += 1
            d = rq.get("tokens_delivered")
            if isinstance(d, (int, float)):
                e["tokens_delivered"] += d
            c = rq.get("tokens_consumed")
            if isinstance(c, (int, float)):
                e["tokens_consumed"] += c
            cost = rq.get("response_cost")
            if isinstance(cost, (int, float)):
                e["cost"] += cost
    rows = [agg[k] for k in order]
    rows.sort(key=lambda e: (-e["served"], -e["requested"], e["model"]))
    return rows


# The label the per-user rollup uses when a delivered record carries no
# user_id (master key / no key store) — same collapse convention as _NO_KEY.
_NO_USER = "(no user)"


def _user_rollup(requests: list) -> list:
    """Per-USER traffic (goal 27): the identity counterpart the per-key rollup
    can't give — a user with two virtual keys is two rows there, one row here.
    Requests, fallbacks, delivered tokens, cost, and how many DISTINCT keys the
    user drove traffic through. Null user_ids collapse into one `(no user)`
    row (honest denominator, never scattered). Busiest first."""
    agg: dict = {}
    order = []
    for rq in requests:
        uid = rq.get("user_id")
        label = uid if uid else _NO_USER
        e = agg.get(label)
        if e is None:
            e = {
                "user_id": uid,
                "requests": 0,
                "fallbacks": 0,
                "tokens": 0,
                "cost": 0.0,
                "_keys": set(),
            }
            agg[label] = e
            order.append(label)
        e["requests"] += 1
        if rq.get("fallback"):
            e["fallbacks"] += 1
        d = rq.get("tokens_delivered")
        if isinstance(d, (int, float)):
            e["tokens"] += d
        cost = rq.get("response_cost")
        if isinstance(cost, (int, float)):
            e["cost"] += cost
        if rq.get("key_alias"):
            e["_keys"].add(rq["key_alias"])
    rows = []
    for label in order:
        e = agg[label]
        e["keys"] = len(e.pop("_keys"))
        rows.append(e)
    rows.sort(key=lambda e: (-e["requests"], e["user_id"] or _NO_USER))
    return rows


def _session_rollup(requests: list) -> list:
    """Per-SESSION rollup (goal 27): the view goal 22's classification and goal
    25's pins were pointing at all along — one row per stickiness key, folding
    that session's turns together: how many, which backends served them, the
    session arm's pin state (pinned backend, pin hits, the one escalation), and
    whether any turn was ENFORCED (goal 26) vs shadow. Only requests that carry
    a stickiness key appear — one-shots have no session to roll up. `requests`
    is newest-first, so the first policy block seen per key is the LATEST pin
    state, and `last_received_at` is the newest turn's sink-arrival stamp.
    Sorted most-recently-active first (unstamped rows sink to the bottom)."""
    agg: dict = {}
    order = []
    for rq in requests:  # newest first (by _requests_view construction)
        sess = rq.get("session") or {}
        key = sess.get("stickiness_key") if isinstance(sess, dict) else None
        if not key:
            continue
        e = agg.get(key)
        if e is None:
            e = {
                "stickiness_key": key,
                "key_source": sess.get("key_source"),
                "turns": 0,
                "backends": [],
                "pinned_backend": None,
                "pin_hits": 0,
                "escalated": False,
                "enforced": False,
                "tokens": 0,
                "cost": 0.0,
                "last_received_at": rq.get("received_at"),
                # Goal 30: identity + title material. key_alias/user_id come
                # from the NEWEST turn (set once, here); first_received_at is
                # overwritten every iteration so it ends on the OLDEST turn;
                # complexity_mix counts buckets across the session's turns.
                # Together these derive the METADATA-ONLY session title —
                # records carry no prompt content by design (docs/09).
                "key_alias": rq.get("key_alias"),
                "user_id": rq.get("user_id"),
                "first_received_at": rq.get("received_at"),
                "complexity_mix": {},
            }
            agg[key] = e
            order.append(key)
        e["turns"] += 1
        # Newest-first stream: the last write per key is the oldest turn.
        e["first_received_at"] = rq.get("received_at")
        cx = rq.get("complexity")
        cx_bucket = cx.get("bucket") if isinstance(cx, dict) else None
        cx_label = cx_bucket if cx_bucket else "unclassified"
        e["complexity_mix"][cx_label] = e["complexity_mix"].get(cx_label, 0) + 1
        srv = rq.get("served_model")
        if srv and srv not in e["backends"]:
            e["backends"].append(srv)
        pol = rq.get("policy")
        if isinstance(pol, dict) and pol.get("arm") == "session":
            # Newest-first: the FIRST session-arm block seen is the freshest
            # pin state — later (older) blocks never overwrite it.
            if e["pinned_backend"] is None and pol.get("pinned_backend"):
                e["pinned_backend"] = pol["pinned_backend"]
            if pol.get("pin_hit"):
                e["pin_hits"] += 1
            if pol.get("escalated"):
                e["escalated"] = True
            if pol.get("enforced"):
                e["enforced"] = True
        d = rq.get("tokens_delivered")
        if isinstance(d, (int, float)):
            e["tokens"] += d
        cost = rq.get("response_cost")
        if isinstance(cost, (int, float)):
            e["cost"] += cost
    rows = [agg[k] for k in order]
    rows.sort(
        key=lambda e: (
            -(e["last_received_at"] or 0),
            -e["turns"],
            e["stickiness_key"],
        )
    )
    return rows


def _backend_rollup(records: list) -> list:
    """Per-BACKEND traffic (goal 27): attempt-trail traffic folded per concrete
    deployment — (backend, api_base) — with its tier, attempt/failure counts,
    tokens, and mean completion latency. This is the closest HONEST per-
    workbench traffic attribution today's data supports: the control-plane
    registry keys workbenches by workbench_id but carries no api_base, so a
    hard attempts→workbench join does not exist yet (queued as a follow-up
    goal — heartbeat gains api_base). In the dev/e2e stacks each workbench IS
    a distinct api_base, so this table already reads per-box. Busiest first."""
    agg: dict = {}
    order = []
    for r in records:
        if r.get("event") != "llm_call":
            continue
        key = (r.get("backend") or "(unknown)", r.get("api_base") or "")
        e = agg.get(key)
        if e is None:
            e = {
                "backend": r.get("backend"),
                "api_base": r.get("api_base"),
                "tier": r.get("tier"),
                "attempts": 0,
                "failures": 0,
                "tokens": 0,
                "_lat_sum": 0.0,
                "_lat_n": 0,
            }
            agg[key] = e
            order.append(key)
        e["attempts"] += 1
        if r.get("status") == "failure":
            e["failures"] += 1
        if e["tier"] is None and r.get("tier"):
            e["tier"] = r.get("tier")
        t = (r.get("tokens") or {}).get("total")
        if isinstance(t, (int, float)):
            e["tokens"] += t
        lat = r.get("latency_ms")
        if isinstance(lat, (int, float)):
            e["_lat_sum"] += lat
            e["_lat_n"] += 1
    rows = []
    for key in order:
        e = agg[key]
        n = e.pop("_lat_n")
        s = e.pop("_lat_sum")
        e["latency_ms_avg"] = round(s / n, 1) if n else None
        rows.append(e)
    rows.sort(key=lambda e: (-e["attempts"], e["backend"] or "", e["api_base"] or ""))
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
    matches NO delivered record. Since goal 29 gave streamed responses a
    delivered record of their own, that is ABORTED/mid-stream-dead streams
    (nothing was delivered — deliberately not a request row) plus requests
    that errored out entirely. Surfaced separately rather than folded into
    the ratio, so the per-request math stays exact and the gap stays VISIBLE
    instead of silently skewing the ratio."""
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
    unattributed_cids = set()
    for r in records:
        if r.get("event") != "llm_call":
            continue
        cid = r.get("correlation_id")
        if cid in delivered_cids:
            continue
        if cid is not None:
            unattributed_cids.add(cid)
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
        # HOW MANY requests the per-request view is BLIND to (goal 27): distinct
        # correlation ids with attempts but no delivered record — since goal 29
        # that is aborted/mid-stream-dead streams plus requests that errored
        # out entirely (a COMPLETED stream delivers like everything else).
        # Surfaced as a COUNT so the dashboard says "N requests happened that
        # the tables below don't show" instead of lying by omission. Attempts
        # with no correlation_id at all can't be grouped into requests and are
        # covered by the token counter above only.
        "unattributed_requests": len(unattributed_cids),
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
    # ENFORCEMENT visibility (goal 27): under ROUTER_POLICY=enforce the block
    # carries enforced:true and "agree" stops meaning "would have" — it means
    # "did the chosen backend actually serve, or did the availability-fallback
    # chain fire AFTER the rewrite" (docs/12 R4). Counted separately so the
    # page can distinguish live routing from shadow opinion at a glance.
    enforced = {"count": 0, "agree": 0, "disagree": 0}
    for r in records:
        if r.get("event") != "delivered":
            continue
        pol = r.get("shadow_policy")
        verdict = pol.get("agree") if isinstance(pol, dict) else None
        if isinstance(pol, dict) and pol.get("enforced"):
            enforced["count"] += 1
            if verdict is True:
                enforced["agree"] += 1
            elif verdict is False:
                enforced["disagree"] += 1
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
        "enforced": enforced,
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

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ctext y='13' font-size='13'%3E%E2%87%86%3C/text%3E%3C/svg%3E">
<title>Router dashboard — where did my prompt go?</title>
<style>
  :root {
    --bg:#0f1117; --panel:#171a23; --line:#262b38; --fg:#e6e9ef; --muted:#8b93a7;
    --ok:#3fb950; --warn:#d29922; --fall:#f0883e; --accent:#58a6ff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { padding:12px 20px; border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    position:sticky; top:0; background:var(--bg); z-index:5; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  .sub { color:var(--muted); font-size:12px; }
  .status { margin-left:auto; color:var(--muted); font-size:12px; }
  /* goal 30: hash-route navigation */
  nav.routes { display:flex; gap:4px; flex-wrap:wrap; }
  nav.routes a { color:var(--muted); text-decoration:none; font-size:13px;
    padding:3px 10px; border-radius:999px; border:1px solid transparent; }
  nav.routes a:hover { color:var(--fg); }
  nav.routes a.active { color:var(--fg); background:var(--panel);
    border-color:var(--line); }
  .wrap { padding:20px; max-width:1200px; }
  section[data-view] { display:none; }
  section[data-view].on { display:block; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
    color:var(--muted); margin:26px 0 10px; }
  h2:first-child { margin-top:0; }
  .h2note { text-transform:none; letter-spacing:0; font-weight:400;
    color:var(--muted); }
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
  a.ent { color:var(--accent); text-decoration:none; }
  a.ent:hover { text-decoration:underline; }
  a.ent code { color:var(--accent); }
  /* goal 30: identifier truncation — one hostile value must never blow up a
     table; the full value lives on the detail view (and title=). */
  .trunc { display:inline-block; max-width:240px; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; vertical-align:bottom; }
  .copy { cursor:pointer; border-bottom:1px dashed var(--line); }
  .copy.copied { border-bottom-color:var(--ok); }
  .strips { color:var(--muted); font-size:12px; margin:0 0 14px;
    line-height:1.8; }
  .crumb { font-size:12px; margin-bottom:12px; }
  .crumb a { color:var(--accent); text-decoration:none; }
  .detailmeta { margin:0 0 14px; line-height:2; }
  .cols { display:flex; gap:16px; flex-wrap:wrap; }
  .cols > div { flex:1 1 420px; min-width:0; }
  /* trace correlation (goal 16): the attempt trail nested UNDER its request */
  tr.trail td { border-bottom:1px solid var(--line); padding:4px 12px 8px;
    white-space:normal; color:var(--muted); font-size:12px; }
  tr.trail .cid { color:var(--muted); }
  /* goal 30: the visible "why" — hover-only tooltips carry nothing exclusive
     anymore on detail views */
  tr.why td { border-bottom:1px solid var(--line); padding:4px 12px 10px;
    white-space:normal; color:var(--muted); font-size:12px; }
  .attempt { display:inline-block; padding:1px 6px; margin:2px 2px;
    border:1px solid var(--line); border-radius:6px; }
  .attempt.failed { border-color:var(--warn); }
  .attempt.ok { border-color:var(--ok); }
</style>
</head>
<body>
<header>
  <h1>Router dashboard</h1>
  <nav class="routes" id="nav">
    <a href="#/overview" data-route="overview">Overview</a>
    <a href="#/traffic" data-route="traffic">Traffic</a>
    <a href="#/identity" data-route="identity">Identity</a>
    <a href="#/sessions" data-route="sessions">Sessions</a>
    <a href="#/requests" data-route="requests">Requests</a>
  </nav>
  <span class="status" id="status">loading&hellip;</span>
</header>
<div class="wrap">

<section data-view="overview">
  <div class="strips" id="ovstrips"></div>
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
</section>

<section data-view="traffic">
  <div class="cols">
    <div>
      <h2>Per model &mdash; traffic: demand vs supply</h2>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>model</th><th class="num">asked</th><th class="num">served</th>
            <th class="num">via&nbsp;fallback</th><th class="num">delivered</th>
            <th class="num">consumed</th><th class="num">cost</th>
          </tr></thead>
          <tbody id="modeltraffic"><tr><td class="empty" colspan="7">no traffic yet</td></tr></tbody>
        </table>
      </div>
    </div>
    <div>
      <h2>Per backend &mdash; deployment traffic <span class="h2note">&middot; per (backend, api_base) &mdash; the per-box view</span></h2>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>backend</th><th>api base</th><th>tier</th><th class="num">attempts</th>
            <th class="num">failures</th><th class="num">tokens</th><th class="num">avg&nbsp;latency</th>
          </tr></thead>
          <tbody id="backends"><tr><td class="empty" colspan="7">no attempts yet</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<section data-view="identity">
  <div class="cols">
    <div>
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
    </div>
    <div>
      <h2>Per user &mdash; across all their keys</h2>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>user</th><th class="num">keys</th><th class="num">requests</th>
            <th class="num">fallbacks</th><th class="num">tokens</th><th class="num">cost</th>
          </tr></thead>
          <tbody id="users"><tr><td class="empty" colspan="6">no traffic yet</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</section>

<section data-view="sessions">
  <h2>Sessions &mdash; sticky keys, pins &amp; the one escalation hop
    <span class="h2note">&middot; one row per stickiness key &middot; titles are metadata-only (no prompt content)</span></h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>session</th><th>source</th><th class="num">turns</th><th>pinned</th>
        <th class="num">pin&nbsp;hits</th><th>escalated</th><th>mode</th><th>backends served</th>
        <th class="num">tokens</th><th class="num">cost</th><th class="num">last&nbsp;seen</th>
      </tr></thead>
      <tbody id="sessions"><tr><td class="empty" colspan="11">no sticky sessions yet</td></tr></tbody>
    </table>
  </div>
</section>

<section data-view="requests">
  <h2>Requests &mdash; requested alias &rarr; backend that served it
    <span class="h2note">&middot; each row nests its own attempt trail</span>
    <span class="h2note" id="overhead"></span>
    <span class="h2note" id="cxdist"></span></h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>requested</th><th></th><th>served</th><th>route</th><th>policy</th><th>complexity</th><th>class</th>
        <th>key</th><th>user</th>
        <th class="num">delivered</th><th class="num">consumed</th><th class="num">cost</th><th class="num">age</th>
      </tr></thead>
      <tbody id="requests"><tr><td class="empty" colspan="13">no requests yet</td></tr></tbody>
    </table>
  </div>

  <h2>Attempt trail &mdash; every backend tried, and why a fallback fired
    <span class="h2note">&middot; ttft = time-to-first-token, streamed only</span></h2>
  <div class="tablewrap">
    <table>
      <thead><tr>
        <th>group</th><th>backend</th><th>tier</th><th>status</th>
        <th class="num">ttft</th><th class="num">latency</th><th class="num">tokens</th><th>error</th><th class="num">age</th>
      </tr></thead>
      <tbody id="attempts"><tr><td class="empty" colspan="9">no attempts yet</td></tr></tbody>
    </table>
  </div>
</section>

<section data-view="user">
  <div class="crumb"><a href="#/identity">&larr; identity</a></div>
  <div id="userdetail"><div class="empty">loading&hellip;</div></div>
</section>

<section data-view="session">
  <div class="crumb"><a href="#/sessions">&larr; sessions</a></div>
  <div id="sessiondetail"><div class="empty">loading&hellip;</div></div>
</section>

</div>

<script>
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
// goal 30: relative sink-arrival age. received_at is stamped when the record
// REACHED the dashboard (records carry no wall clock of their own).
function ago(ts){
  if(ts==null) return '&mdash;';
  const s = Math.max(0, Math.round(Date.now()/1000 - ts));
  if(s < 90) return s+' s';
  if(s < 5400) return Math.round(s/60)+' m';
  return Math.round(s/3600)+' h';
}
function hhmm(ts){
  if(ts==null) return '?';
  const d = new Date(ts*1000);
  return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
}
// goal 30: the METADATA-ONLY session title — records carry no prompt content
// by design (data governance), so a session is named by who/when/what-shape:
// "repo-a · 14:02 · agentic · 3 turns".
function sessTitle(s){
  const mix = s.complexity_mix || {};
  let dom = null, max = 0;
  for(const k of Object.keys(mix)){
    if(k !== 'unclassified' && mix[k] > max){ dom = k; max = mix[k]; }
  }
  return (s.key_alias || 'unkeyed') + ' · ' + hhmm(s.first_received_at)
    + (dom ? ' · ' + dom : '') + ' · ' + s.turns + ' turn' + (s.turns===1?'':'s');
}
function userHref(u){ return '#/user/'+encodeURIComponent(u); }
function sessHref(k){ return '#/session/'+encodeURIComponent(k); }
function idCell(v){
  return v==null ? '<span class="muted">&mdash;</span>' : '<code class="trunc" title="'+esc(v)+'">'+esc(v)+'</code>';
}
// Identity cell that LINKS to the user drill-down (goal 30).
function userCell(v){
  if(v==null) return '<span class="muted">&mdash;</span>';
  return '<a class="ent" href="'+userHref(v)+'"><code class="trunc" title="'+esc(v)+'">'+esc(v)+'</code></a>';
}
function attemptChip(a){
  const failed = a.status==='failure';
  const tier = a.tier ? ' <span class="badge tier">'+esc(a.tier)+'</span>' : '';
  const lat = a.latency_ms==null ? ''
    : ' <span class="muted">'+(a.ttft_ms==null?'':esc(a.ttft_ms)+'ms ttft / ')+esc(a.latency_ms)+'ms</span>';
  const mark = failed
    ? ' <span class="badge fail">'+esc(a.error_code||'fail')+'</span>'
    : ' <span class="badge direct">ok</span>';
  return '<span class="attempt '+(failed?'failed':'ok')+'"><code>'
    + esc(a.backend||a.requested_group)+'</code>'+tier+mark+lat+'</span>';
}
function trailChips(r){
  const atts = r.attempts||[];
  if(!atts.length) return '';
  return atts.map(attemptChip).join('<span class="arrow"> &rarr; </span>');
}
function reqTrail(r, span){
  const chips = trailChips(r);
  if(!chips) return '';
  const cid = r.correlation_id ? ' <span class="cid">('+esc(r.correlation_id)+')</span>' : '';
  return '<tr class="trail"><td></td><td colspan="'+span+'">why'+cid+': '+chips+'</td></tr>';
}
// Routing policy (goals 24/25/26): enforced (LIVE routing) rendered apart from
// shadow opinion; pin/esc chips carry goal 25's session state. Full citable
// reason on hover here — and as VISIBLE text on the session detail (goal 30).
function polWhy(p){
  const bits = ['arm '+(p.arm||'?')];
  if(p.requested) bits.push('asked '+p.requested);
  bits.push('policy chose '+(p.chosen||'(none)'));
  if(p.pinned_backend) bits.push('pin '+p.pinned_backend);
  bits.push('registry '+(p.registry||'?'));
  if(p.reason) bits.push(p.reason);
  return bits.join(' · ');
}
function polCell(p){
  if(!p) return '<span class="muted">&mdash;</span>';
  const why = polWhy(p);
  const chips =
    (p.pin_hit ? ' <span class="badge tier" title="served from the sticky pin">pin</span>' : '')
    +(p.escalated ? ' <span class="badge fall" title="this session took its one upward hop'
        +(p.escalated_from ? ' from '+esc(p.escalated_from) : '')+'">esc</span>' : '');
  if(p.enforced){
    if(p.agree===false)
      return '<span class="badge fall" title="'+esc(why)+'">enforced&middot;drift</span>'+chips;
    return '<span class="badge yes" title="'+esc(why)+'">enforced</span>'+chips;
  }
  if(p.agree===true)
    return '<span class="badge direct" title="'+esc(why)+'">agree</span>'+chips;
  if(p.agree===false)
    return '<span class="badge fall" title="'+esc(why)+'">chose '+esc(p.chosen)+'</span>'+chips;
  return '<span class="badge no" title="'+esc(why)+'">no verdict</span>'+chips;
}
function sessCell(s){
  if(!s || !s.request_class) return '<span class="muted">&mdash;</span>';
  const isSession = s.request_class==='session-turn';
  const cls = isSession ? 'yes' : 'no';
  const why = s.stickiness_key
    ? ('key '+s.stickiness_key+' ('+(s.key_source||'?')+')')
    : 'no stickiness key';
  const badge = '<span class="badge '+cls+'" title="'+esc(why)+'">'+esc(s.request_class)+'</span>';
  // A session-turn links straight into its session's drill-down (goal 30).
  return s.stickiness_key ? '<a class="ent" href="'+sessHref(s.stickiness_key)+'">'+badge+'</a>' : badge;
}
const CX_CLASS = {trivial:'no', toolful:'tier', heavy:'fail', agentic:'fall'};
function cxWhy(cx){
  return '~'+cx.approx_prompt_tokens+' tok / '+cx.turns+' turns / '+cx.tools+' tools';
}
function cxCell(cx){
  if(!cx || !cx.bucket) return '<span class="muted">&mdash;</span>';
  const cls = CX_CLASS[cx.bucket] || 'no';
  return '<span class="badge '+cls+'" title="'+esc(cxWhy(cx))+'">'+esc(cx.bucket)+'</span>';
}
function reqCells(r){
  const badge = r.fallback
    ? '<span class="badge fall">fallback</span>'
    : '<span class="badge direct">direct</span>';
  const del = r.tokens_delivered==null ? '&mdash;' : esc(r.tokens_delivered);
  const over = r.tokens_consumed!=null && r.tokens_consumed > (r.tokens_delivered||0);
  const con = r.tokens_consumed==null ? '&mdash;'
    : (over ? '<span class="badge fall">'+esc(r.tokens_consumed)+'</span>' : esc(r.tokens_consumed));
  const cost = r.response_cost==null ? '&mdash;' : ('$'+Number(r.response_cost).toFixed(6));
  return { badge, del, con, cost };
}
function reqRow(r){
  const c = reqCells(r);
  return '<tr>'
    + '<td><code>'+esc(r.requested_model)+'</code></td>'
    + '<td class="arrow">&rarr;</td>'
    + '<td><code>'+esc(r.served_model)+'</code></td>'
    + '<td>'+c.badge+'</td>'
    + '<td>'+polCell(r.policy)+'</td>'
    + '<td>'+cxCell(r.complexity)+'</td>'
    + '<td>'+sessCell(r.session)+'</td>'
    + '<td>'+idCell(r.key_alias)+'</td>'
    + '<td>'+userCell(r.user_id)+'</td>'
    + '<td class="num">'+c.del+'</td>'
    + '<td class="num">'+c.con+'</td>'
    + '<td class="num">'+c.cost+'</td>'
    + '<td class="num muted">'+ago(r.received_at)+'</td>'
    + '</tr>'
    + reqTrail(r, 12);
}
function keyRow(k){
  const alias = k.key_alias==null
    ? '<span class="muted">master key / no key</span>'
    : '<code class="trunc" title="'+esc(k.key_alias)+'">'+esc(k.key_alias)+'</code>';
  const fb = k.fallbacks
    ? '<span class="badge fall">'+esc(k.fallbacks)+'</span>' : '0';
  const cost = (k.cost==null) ? '&mdash;' : ('$'+Number(k.cost).toFixed(6));
  return '<tr>'
    + '<td>'+alias+'</td>'
    + '<td>'+userCell(k.user_id)+'</td>'
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
    + '<td class="num muted">'+ago(a.received_at)+'</td>'
    + '</tr>';
}
function modelRow(m){
  const over = m.tokens_consumed > m.tokens_delivered;
  const con = over
    ? '<span class="badge fall">'+esc(m.tokens_consumed)+'</span>'
    : esc(m.tokens_consumed);
  const fb = m.fallbacks_in
    ? '<span class="badge fall">'+esc(m.fallbacks_in)+'</span>' : '0';
  return '<tr>'
    + '<td><code>'+esc(m.model)+'</code></td>'
    + '<td class="num">'+esc(m.requested)+'</td>'
    + '<td class="num">'+esc(m.served)+'</td>'
    + '<td class="num">'+fb+'</td>'
    + '<td class="num">'+esc(m.tokens_delivered)+'</td>'
    + '<td class="num">'+con+'</td>'
    + '<td class="num">$'+Number(m.cost||0).toFixed(6)+'</td>'
    + '</tr>';
}
function userRow(u){
  const fb = u.fallbacks
    ? '<span class="badge fall">'+esc(u.fallbacks)+'</span>' : '0';
  return '<tr>'
    + '<td>'+(u.user_id==null ? '<span class="muted">no user</span>' : userCell(u.user_id))+'</td>'
    + '<td class="num">'+esc(u.keys)+'</td>'
    + '<td class="num">'+esc(u.requests)+'</td>'
    + '<td class="num">'+fb+'</td>'
    + '<td class="num">'+esc(u.tokens)+'</td>'
    + '<td class="num">$'+Number(u.cost||0).toFixed(6)+'</td>'
    + '</tr>';
}
function backendRow(b){
  const fails = b.failures
    ? '<span class="badge fail">'+esc(b.failures)+'</span>' : '0';
  const tier = b.tier ? '<span class="badge tier">'+esc(b.tier)+'</span>' : '&mdash;';
  const lat = b.latency_ms_avg==null ? '&mdash;' : esc(b.latency_ms_avg)+' ms';
  return '<tr>'
    + '<td><code>'+esc(b.backend)+'</code></td>'
    + '<td class="muted"><span class="trunc" title="'+esc(b.api_base||'')+'">'+esc(b.api_base||'')+'</span></td>'
    + '<td>'+tier+'</td>'
    + '<td class="num">'+esc(b.attempts)+'</td>'
    + '<td class="num">'+fails+'</td>'
    + '<td class="num">'+esc(b.tokens)+'</td>'
    + '<td class="num">'+lat+'</td>'
    + '</tr>';
}
function sessionStateCells(s){
  const pinned = s.pinned_backend
    ? '<code>'+esc(s.pinned_backend)+'</code>' : '<span class="muted">&mdash;</span>';
  const escd = s.escalated
    ? '<span class="badge fall">esc</span>' : '<span class="badge no">no</span>';
  const mode = s.enforced
    ? '<span class="badge yes">enforced</span>' : '<span class="badge no">shadow</span>';
  return { pinned, escd, mode };
}
function sessionRow(s){
  const c = sessionStateCells(s);
  return '<tr>'
    + '<td><a class="ent" href="'+sessHref(s.stickiness_key)+'" title="'+esc(s.stickiness_key)+'">'+esc(sessTitle(s))+'</a></td>'
    + '<td class="muted">'+esc(s.key_source||'&mdash;')+'</td>'
    + '<td class="num">'+esc(s.turns)+'</td>'
    + '<td>'+c.pinned+'</td>'
    + '<td class="num">'+esc(s.pin_hits)+'</td>'
    + '<td>'+c.escd+'</td>'
    + '<td>'+c.mode+'</td>'
    + '<td>'+(s.backends||[]).map(b=>'<code>'+esc(b)+'</code>').join(' ')+'</td>'
    + '<td class="num">'+esc(s.tokens)+'</td>'
    + '<td class="num">$'+Number(s.cost||0).toFixed(6)+'</td>'
    + '<td class="num">'+ago(s.last_received_at)+'</td>'
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

// --- the strips (shared by overview + requests) ------------------------------
function overheadStrip(data){
  const ov = data.overhead;
  if(!ov || !ov.requests && !ov.unattributed_requests) return '';
  return '&Sigma; delivered '+esc(ov.tokens_delivered)
    +' / consumed '+esc(ov.tokens_consumed)
    +(ov.overhead_ratio!=null ? ' (ratio '+esc(ov.overhead_ratio)+')' : '')
    +(ov.unattributed_requests ? ' &middot; '+esc(ov.unattributed_requests)+' req / '
      +esc(ov.unattributed_attempt_tokens)+' tok unattributed (aborted/errored &mdash; NOT in these tables)'
      : (ov.unattributed_attempt_tokens ? ' &middot; +'+esc(ov.unattributed_attempt_tokens)+' unattributed (aborted/errored)' : ''));
}
function mixStrip(data){
  const cx = data.complexity_buckets || {};
  const mix = Object.keys(cx).sort().map(k=>esc(k)+' '+esc(cx[k])).join(' / ');
  const rc = data.request_classes || {};
  const cmix = Object.keys(rc).sort().map(k=>esc(k)+' '+esc(rc[k])).join(' / ');
  const pa = data.policy_agreement;
  const enf = (pa && pa.enforced && pa.enforced.count)
    ? ', '+esc(pa.enforced.count)+' enforced'
      +(pa.enforced.disagree ? ' ('+esc(pa.enforced.disagree)+' drift)' : '')
    : '';
  const pmix = (pa && pa.evaluated)
    ? ' &middot; policy: '+esc(pa.agree)+'/'+esc(pa.evaluated)+' agree'
      +(pa.agreement_rate!=null ? ' ('+esc(pa.agreement_rate)+')' : '')
      +(pa.unevaluated ? ', '+esc(pa.unevaluated)+' unevaluated' : '')
      + enf
    : '';
  return (mix ? 'mix: '+mix : '') + (cmix ? ' &middot; class: '+cmix : '') + pmix;
}

// --- detail views (goal 30) ---------------------------------------------------
function copySpan(v){
  return '<span class="copy" data-copy="'+esc(v)+'" title="click to copy">'+esc(v)+'</span>';
}
// One turn of a session, with its trail AND the visible "why" — the policy's
// full reason and the complexity features as text, not hover-only (goal 30).
function turnRows(r){
  const c = reqCells(r);
  let why = [];
  if(r.policy) why.push('policy: '+polWhy(r.policy));
  if(r.complexity && r.complexity.bucket) why.push('complexity: '+r.complexity.bucket+' ('+cxWhy(r.complexity)+')');
  const chips = trailChips(r);
  const whyRow = (why.length || chips)
    ? '<tr class="why"><td></td><td colspan="9">'
      + (chips ? 'trail: '+chips+'<br>' : '')
      + esc(why.join(' · '))
      + '</td></tr>'
    : '';
  return '<tr>'
    + '<td class="num muted">'+ago(r.received_at)+'</td>'
    + '<td><code>'+esc(r.requested_model)+'</code></td>'
    + '<td class="arrow">&rarr;</td>'
    + '<td><code>'+esc(r.served_model)+'</code></td>'
    + '<td>'+c.badge+'</td>'
    + '<td>'+polCell(r.policy)+'</td>'
    + '<td class="num">'+c.del+'</td>'
    + '<td class="num">'+c.con+'</td>'
    + '<td class="num">'+c.cost+'</td>'
    + '<td></td>'
    + '</tr>' + whyRow;
}
function renderSessionDetail(key){
  const box = document.getElementById('sessiondetail');
  const sessions = DATA.sessions||[];
  const s = sessions.find(x=>x.stickiness_key===key);
  const turns = (DATA.requests||[]).filter(r=>r.session && r.session.stickiness_key===key);
  if(!s && !turns.length){
    box.innerHTML = '<h2>Session</h2><div class="empty">no records for session key <code>'+esc(key)+'</code> (expired from the sink, or never seen)</div>';
    return;
  }
  const state = s ? sessionStateCells(s) : null;
  let head = '<h2>'+(s ? esc(sessTitle(s)) : 'Session')+'</h2>';
  head += '<div class="detailmeta muted">key '+copySpan(key)
    + (s && s.key_source ? ' &middot; source '+esc(s.key_source) : '')
    + (s && s.user_id ? ' &middot; user <a class="ent" href="'+userHref(s.user_id)+'">'+esc(s.user_id)+'</a>' : '')
    + (s && s.key_alias ? ' &middot; key alias <code>'+esc(s.key_alias)+'</code>' : '')
    + '</div>';
  if(state){
    head += '<div class="detailmeta">pinned '+state.pinned
      + ' &middot; pin hits '+esc(s.pin_hits)
      + ' &middot; escalated '+state.escd
      + ' &middot; mode '+state.mode
      + ' &middot; backends '+(s.backends||[]).map(b=>'<code>'+esc(b)+'</code>').join(' ')
      + ' &middot; '+esc(s.tokens)+' tok &middot; $'+Number(s.cost||0).toFixed(6)
      + '</div>';
  }
  const rows = turns.map(turnRows).join('');
  box.innerHTML = head
    + '<div class="tablewrap"><table>'
    + '<thead><tr><th class="num">age</th><th>requested</th><th></th><th>served</th><th>route</th><th>policy</th>'
    + '<th class="num">delivered</th><th class="num">consumed</th><th class="num">cost</th><th></th></tr></thead>'
    + '<tbody>'+(rows || '<tr><td class="empty" colspan="10">no turns visible (records may have expired from the sink)</td></tr>')+'</tbody>'
    + '</table></div>';
}
function renderUserDetail(uid){
  const box = document.getElementById('userdetail');
  const reqs = (DATA.requests||[]).filter(r=>r.user_id===uid);
  const keys = (DATA.keys||[]).filter(k=>k.user_id===uid);
  const sessions = (DATA.sessions||[]).filter(s=>s.user_id===uid);
  const u = (DATA.users||[]).find(x=>x.user_id===uid);
  let head = '<h2>User '+copySpan(uid)+'</h2>';
  if(u){
    head += '<div class="detailmeta muted">'+esc(u.requests)+' requests across '+esc(u.keys)
      + ' key'+(u.keys===1?'':'s')+' &middot; '+esc(u.fallbacks)+' fallbacks &middot; '
      + esc(u.tokens)+' tok &middot; $'+Number(u.cost||0).toFixed(6)+'</div>';
  }
  let html = head;
  html += '<h2>Keys</h2><div class="tablewrap"><table>'
    + '<thead><tr><th>key</th><th>user</th><th>team</th><th class="num">requests</th><th class="num">fallbacks</th><th class="num">tokens</th><th class="num">cost</th></tr></thead>'
    + '<tbody>'+(keys.length ? keys.map(keyRow).join('') : '<tr><td class="empty" colspan="7">no keyed traffic</td></tr>')+'</tbody></table></div>';
  html += '<h2>Sessions</h2><div class="tablewrap"><table>'
    + '<thead><tr><th>session</th><th>source</th><th class="num">turns</th><th>pinned</th><th class="num">pin&nbsp;hits</th><th>escalated</th><th>mode</th><th>backends served</th><th class="num">tokens</th><th class="num">cost</th><th class="num">last&nbsp;seen</th></tr></thead>'
    + '<tbody>'+(sessions.length ? sessions.map(sessionRow).join('') : '<tr><td class="empty" colspan="11">no sticky sessions</td></tr>')+'</tbody></table></div>';
  html += '<h2>Requests</h2><div class="tablewrap"><table>'
    + '<thead><tr><th>requested</th><th></th><th>served</th><th>route</th><th>policy</th><th>complexity</th><th>class</th><th>key</th><th>user</th><th class="num">delivered</th><th class="num">consumed</th><th class="num">cost</th><th class="num">age</th></tr></thead>'
    + '<tbody>'+(reqs.length ? reqs.map(reqRow).join('') : '<tr><td class="empty" colspan="13">no requests</td></tr>')+'</tbody></table></div>';
  box.innerHTML = html;
}

// --- router + render loop (goal 30) -------------------------------------------
let DATA = {}, FLEET = {};
function parseRoute(){
  const h = location.hash || '#/overview';
  let m = h.match(/^#\/(overview|traffic|identity|sessions|requests)$/);
  if(m) return { view: m[1], arg: null };
  m = h.match(/^#\/user\/(.+)$/);
  if(m) return { view: 'user', arg: decodeURIComponent(m[1]) };
  m = h.match(/^#\/session\/(.+)$/);
  if(m) return { view: 'session', arg: decodeURIComponent(m[1]) };
  return { view: 'overview', arg: null };
}
function renderView(){
  const r = parseRoute();
  document.querySelectorAll('section[data-view]').forEach(s =>
    s.classList.toggle('on', s.dataset.view === r.view));
  document.querySelectorAll('#nav a').forEach(a =>
    a.classList.toggle('active', a.dataset.route === r.view));
  const data = DATA;
  if(r.view === 'overview'){
    const oh = overheadStrip(data), mx = mixStrip(data);
    document.getElementById('ovstrips').innerHTML =
      (oh ? oh + '<br>' : '') + mx;
    renderFleet();
  } else if(r.view === 'traffic'){
    const models = data.models||[], backends = data.backends||[];
    document.getElementById('modeltraffic').innerHTML = models.length
      ? models.map(modelRow).join('')
      : '<tr><td class="empty" colspan="7">no traffic yet</td></tr>';
    document.getElementById('backends').innerHTML = backends.length
      ? backends.map(backendRow).join('')
      : '<tr><td class="empty" colspan="7">no attempts yet</td></tr>';
  } else if(r.view === 'identity'){
    const keys = data.keys||[], users = data.users||[];
    document.getElementById('keys').innerHTML = keys.length
      ? keys.map(keyRow).join('')
      : '<tr><td class="empty" colspan="7">no keyed traffic yet</td></tr>';
    document.getElementById('users').innerHTML = users.length
      ? users.map(userRow).join('')
      : '<tr><td class="empty" colspan="6">no traffic yet</td></tr>';
  } else if(r.view === 'sessions'){
    const sessions = data.sessions||[];
    document.getElementById('sessions').innerHTML = sessions.length
      ? sessions.map(sessionRow).join('')
      : '<tr><td class="empty" colspan="11">no sticky sessions yet</td></tr>';
  } else if(r.view === 'requests'){
    const reqs = data.requests||[], atts = data.attempts||[];
    document.getElementById('requests').innerHTML = reqs.length
      ? reqs.map(reqRow).join('')
      : '<tr><td class="empty" colspan="13">no requests yet</td></tr>';
    document.getElementById('attempts').innerHTML = atts.length
      ? atts.map(attRow).join('')
      : '<tr><td class="empty" colspan="9">no attempts yet</td></tr>';
    const oh = overheadStrip(data);
    document.getElementById('overhead').innerHTML = oh ? '&middot; '+oh : '';
    const mx = mixStrip(data);
    document.getElementById('cxdist').innerHTML = mx ? '&middot; '+mx : '';
  } else if(r.view === 'user'){
    renderUserDetail(r.arg);
  } else if(r.view === 'session'){
    renderSessionDetail(r.arg);
  }
}
function renderFleet(){
  const fs = document.getElementById('fleetstatus');
  const data = FLEET;
  if(!data.available){
    fs.textContent = '— fleet unavailable ('+(data.error||'no control-plane')+')';
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
}
async function tick(){
  try {
    const res = await fetch('api/records', {cache:'no-store'});
    DATA = await res.json();
    const ov = DATA.overhead||{};
    document.getElementById('status').textContent =
      (DATA.requests||[]).length+' requests · '+(DATA.attempts||[]).length+' attempts'
      +(ov.unattributed_requests ? ' · +'+ov.unattributed_requests+' unattributed' : '')
      +' · '+(DATA.count||0)+' records';
  } catch(e) {
    document.getElementById('status').textContent = 'sink unreachable';
  }
  try {
    const res = await fetch('api/fleet', {cache:'no-store'});
    FLEET = await res.json();
  } catch(e) {
    FLEET = {available:false, error:'fleet endpoint error'};
  }
  renderView();
}
window.addEventListener('hashchange', renderView);
document.addEventListener('click', e => {
  const c = e.target.closest('[data-copy]');
  if(!c) return;
  if(navigator.clipboard) navigator.clipboard.writeText(c.dataset.copy);
  c.classList.add('copied');
  setTimeout(()=>c.classList.remove('copied'), 600);
});
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
        # Goal 30: never let a browser cache the page shell — a dashboard
        # upgrade must show on the next load, not after a hard refresh
        # (observed live: a stale pre-v4 page served from browser cache).
        self.send_header("Cache-Control", "no-store")
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
                    # goal 27: the per-dimension rollups. `models` here is
                    # TRAFFIC per model (demand vs supply) — fleet CAPACITY
                    # per model lives at /api/fleet.
                    "models": _model_rollup(requests),
                    "users": _user_rollup(requests),
                    "sessions": _session_rollup(requests),
                    "backends": _backend_rollup(recs),
                    "records": recs,
                },
            )
        # Goal 13 — the fleet DATA ENDPOINT the UI fetches and the e2e assertion
        # covers: a server-side read of the control-plane registry. Always 200,
        # even when the control-plane is down (available:false in the body).
        if self.path.startswith("/api/fleet"):
            return self._json(200, _fetch_fleet())
        # Query strings tolerated (e.g. a cache-busting /?x) — route on the
        # bare path (goal 30; observed live: GET /?v4 404'd).
        path = self.path.split("?", 1)[0]
        if path == "/" or path.startswith("/index") or path.startswith("/dashboard"):
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
