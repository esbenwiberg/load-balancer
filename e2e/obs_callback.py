"""Routing-record observability callback (goal 3).

Answers "where did my prompt go, why, how long, how many tokens, and did it
fall back?" for every request through the gateway — with NO external
observability stack (no Langfuse, no OTEL collector, no Postgres read).

It emits two record shapes, keyed by `event`:

  * llm_call  — one per BACKEND ATTEMPT (success OR failure). Carries the
                backend that was tried, its tier, per-attempt latency (time to
                COMPLETION), tokens, and — on failure — the error that TRIGGERED
                a fallback (the "why", e.g. a 503/429). This is the attempt trail.

                TTFT (goal 18): a STREAMED attempt also carries `ttft_ms`, the
                time-to-first-token (the FELT latency for an agent), read from
                LiteLLM's own completionStartTime timestamp. Non-streamed records
                OMIT it. By construction ttft_ms <= latency_ms. See _ttft_ms /
                _latency_ms below and docs/09-observability.md.

                ⚠️ LiteLLM quirk (verified against v1.83.14): on a NON-STREAMED
                proxy fallback the WINNING deployment does NOT fire a success
                event — only the failed primary attempt logs here; the winner
                is captured by the `delivered` record below instead. A consumed
                STREAM's winner always fires its success event (the goal-29
                research — that event is what builds the streamed delivered
                record). See docs/09-observability.md.

  * delivered — one per CLIENT REQUEST: the final response handed back. Carries
                the alias the client ASKED for vs the backend that actually
                SERVED it, so `fallback = requested_model != served_model`, plus
                the delivered tokens and cost. This reliably names the chosen
                backend even when it was reached via fallback.

                WHO asked (goal 15): also carries {key_alias, user_id, team_id}
                sourced from the request's UserAPIKeyAuth — the virtual key's
                alias and the user/team it was minted for (goal 11b). All three
                are null under the master key / no key store, so the bare-pytest
                and cli-auth profiles are unaffected.

                STREAMED responses included (goal 29): a stream the client
                consumed to the end yields a delivered record too, built from
                the post-stream success event rather than the post-call hook
                (which structurally never runs for streams on this pin) and
                marked `stream: true`. Requested-vs-served, identity and the
                session tag come from a pre-call context stash keyed by the
                goal-16 correlation id. See _delivered_stream_record.

TRACE CORRELATION — joining a request to its attempt trail (goal 16):

  Every record carries a `correlation_id` so the dashboard can NEST each
  `delivered` request under its `llm_call` attempts instead of showing them side
  by side. The id is LiteLLM's request-scoped `litellm_trace_id`, which the router
  SHARES across a whole fallback group — its `_update_kwargs_before_fallbacks`
  sets it ONCE, before the fallback loop, via setdefault — so the failed primary
  attempt AND the winner carry the SAME trace_id.

  The gap this closes: that shared trace_id already reaches the `llm_call` records
  (it's their `standard_logging_object.trace_id`), but on the proxy fallback path
  the WINNER'S success event is not reliably fired (the verified quirk, below) and
  the delivered response's `_hidden_params` does NOT expose the trace_id — so the
  `delivered` record, built in async_post_call_success_hook, had no id to join on.

  Fix (NO gateway fork — this lives entirely in our own callback): async_pre_call_hook
  STAMPS `data["litellm_trace_id"]` at ingress. Because the router uses setdefault,
  our id becomes THE shared trace_id for every attempt; and because the proxy threads
  the SAME `data` dict through pre-call -> the LLM call -> async_post_call_success_hook,
  the delivered record reads it straight back off `data`. Result: a guaranteed shared
  `correlation_id` linking a request to ALL its attempts (including a fallback's failed
  primary), on both the direct and fallback paths. A client-supplied litellm_trace_id
  (or an x-litellm-trace-id header) is preserved, never overwritten. See docs/09.

SHADOW COMPLEXITY (goal 21): both record shapes also carry a `complexity` tag —
a deterministic decision-tree classification (trivial/toolful/heavy/agentic)
over request features only, with the full feature vector on the record so it is
auditable. Computed in the logging hooks AFTER routing: pure telemetry for the
future task-aware router, zero influence on anything. See _complexity + docs/09.

SHADOW SESSION CLASSIFICATION (goal 22): both record shapes also carry a
`session` tag — {request_class: session-turn|one-shot, stickiness_key,
key_source: tag|transcript|null} — the telemetry backing the decided HYBRID
routing granularity (docs/03): sticky sessions vs freely-routed one-shots.
Same shadow discipline as goal 21. See _session + docs/09.

SHADOW ROUTING POLICY — the stateless arm (goal 24): the first BUILT brick of
the hybrid router (docs/12 §4), still in shadow. async_pre_call_hook computes,
BEFORE routing, what the stateless cheapest-capable policy WOULD have chosen —
applying docs/12 §4's order verbatim:

  1. governance — the key's model allowlist (LiteLLM's key-scoped `models`,
     read off UserAPIKeyAuth) filters the candidate set;
  2. agent_capable gate — toolful/agentic complexity buckets require an
     agent_capable backend (registry verdict when the model is registered,
     config model_info otherwise);
  3. health — control-plane derived `healthy` (docs/10 D3) excludes registered-
     but-unhealthy backends; models the registry has never seen pass on config
     (Foundry backends never heartbeat — only workbenches do);
  4. cheaper tier first (local < foundry), tie-break lowest in_flight
     (control-plane), then name — fully deterministic.

The decision rides the routing record as `shadow_policy: {arm: "stateless",
candidate_set, chosen, reason, registry, actual, agree}` — chosen (what the
policy would do) next to actual (what really happened), so its choices are
auditable against reality before anything enforces. ZERO routing influence:
the hook never touches data["model"], never buffers a stream; a policy error
degrades to "no block", never a failed request.

When the control-plane registry is ABSENT (no CONTROL_PLANE_URL / unreachable
with no recent snapshot) or STALE (unreachable and the last snapshot outlived
POLICY_REGISTRY_STALE_S), the policy degrades to config-only candidates and the
block's `registry` field says so ("absent"/"stale" vs "live"). Registry reads
are TTL-cached (POLICY_REGISTRY_CACHE_S; the e2e stack sets 0 for determinism)
with a short timeout so a hung control-plane costs bounded pre-call latency.

SHADOW STICKY PINS + ESCALATION MECHANICS — the session arm (goal 25): when a
request carries a stickiness key (goal 22's derivation: session tag >
transcript hash), the shadow policy switches to docs/12 §2/§3/§5's session
arm. First sight of a key PINS the stateless arm's choice in a gateway-local,
TTL'd pin store (docs/12 §3 option (a) — the decided default for the
single-gateway phase; a container-scoped SQLite file, POLICY_PIN_DB, because
every profile runs the proxy with --num_workers 2 and per-process memory
would give each worker its own contradictory pins; lost on container restart
BY DESIGN, an unpinned session-turn just re-pins). Subsequent same-key
requests carry the pin, bypassing re-evaluation:
`shadow_policy: {arm: "session", stickiness_key, pin_hit, pinned_backend,
escalated, chosen, ...}`. An explicit `escalate` tag on the verified carrier
(x-litellm-tags) fires docs/12 §5's state machine IN SHADOW: the pin is
replaced upward (local tier -> foundry tier) exactly once — the escalation
target is the stateless arm re-run over the strictly-higher tiers, so
governance/gate/health still apply — no downward edge ever, and any further
signal is a recorded no-op. The escalate tag is a STUB trigger: it proves the
mechanics; the real trigger decision stays open (GOALS.md § Needs-a-human).
Same shadow discipline throughout: zero routing influence, and the pin store
is the SHADOW router's own state — it records what the policy WOULD have
pinned, not what actually served. See _PinStore/_policy_session + docs/09.

ENFORCEMENT (goal 26): the ROUTER_POLICY knob flips the policy from shadow to
real. Default "shadow" = everything above unchanged, zero influence. Under
"enforce" the pre-call hook REWRITES data["model"] to the policy's chosen
backend (both arms, stub escalation included), stashing the client's original
ask on the block FIRST — post-rewrite nothing downstream can reconstruct it
(verified on the pin, docs/12 §7 goal-26 addendum). The block then carries
{enforced: true, requested, chosen, actual, agree}: the full requested vs
chosen vs served triple. The delivered record's top-level requested_model
shows the POST-policy model under enforce (it reads data["model"]), and the
`fallback` flag keeps meaning "the availability chain fired" (served !=
routed-for model) — the client-level triple lives on the policy block.
LiteLLM's availability-fallback composes with the rewrite (R4, verified: the
CHOSEN model's chain applies) and the key allowlist is NOT re-checked after
the rewrite, so the policy's governance filter is the sole guard — pinned by
a dedicated e2e test. A block with no survivor rewrites nothing (degrade to
the client's ask, never a failure). See _apply_enforcement + docs/09.

The block crosses hook boundaries via the goal-16 correlation id in a bounded
module-level map (_POLICY_BLOCKS) — the id is already proven to reach every
surface (delivered via data, attempts via slo.trace_id), unlike the metadata
dict whose shape varies across the three inbound protocols. The delivered
record carries the authoritative actual/agree (actual = served_model) — since
goal 29 for streamed requests too; attempt records carry the block best-effort
(actual = the attempt's backend on success). See _policy_stateless + docs/09.

Sinks (independent, both optional):

  * stdout  — ALWAYS. One JSON object per line, prefixed `ROUTING_RECORD `.
              This is the production-friendly, dependency-free path: scrape it
              with `docker logs ... | grep ROUTING_RECORD` or ship it to any log
              collector. See docs/09-observability.md.
  * webhook — only if OBS_WEBHOOK_URL is set: POST each record there. Accepts a
              COMMA-SEPARATED list of URLs and fans the record out to every one,
              independently — so a single record can land in more than one sink.
              The e2e stack points this at BOTH mockd's /__observe (so the goal-3
              suite can read records back) AND the dashboard's /records (goal 12,
              the "where did my prompt go?" viewer). Fire-and-forget with a short
              timeout; ANY failure on ANY sink is swallowed so observability can
              never break the request path (logging runs post-response anyway).

Wire-up (litellm-config.*.yaml):  litellm_settings: { callbacks: obs_callback.routing_recorder }
The file must sit next to the config so LiteLLM can import it (it adds the
config dir to sys.path).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import OrderedDict

from litellm.integrations.custom_logger import CustomLogger

try:  # httpx ships with litellm; guard anyway so an import hiccup can't wedge boot.
    import httpx
except Exception:  # pragma: no cover - defensive
    httpx = None

# One or more sinks, comma-separated. Blanks are dropped so a trailing comma or
# an unset value can't produce a bogus empty URL.
_WEBHOOK_URLS = [
    u.strip() for u in os.environ.get("OBS_WEBHOOK_URL", "").split(",") if u.strip()
]
_WEBHOOK_TIMEOUT = float(os.environ.get("OBS_WEBHOOK_TIMEOUT", "2.0"))
_STDOUT_PREFIX = "ROUTING_RECORD "

# LiteLLM's request-scoped id. The router shares it across a whole fallback group
# (setdefault before the fallback loop), so stamping it at ingress gives every
# attempt AND the delivered summary the same value to join on. See the module
# docstring + docs/09.
_CORRELATION_KEY = "litellm_trace_id"


def _first(d, *keys):
    """First non-None value across a chain of nested dict lookups."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


def _emit(record: dict) -> None:
    """Publish one record to every configured sink. Never raises."""
    # stdout — always, and first, so a webhook failure can't cost us the record.
    try:
        print(_STDOUT_PREFIX + json.dumps(record, default=str), flush=True)
    except Exception:  # pragma: no cover - defensive
        pass
    if not _WEBHOOK_URLS or httpx is None:
        return
    # Blocking POST to each local sink (mockd/dashboard are instant); runs
    # post-response so it adds no client latency. Each sink is independent — an
    # error on one must not starve the others, and any error is swallowed:
    # observability is best-effort and must never surface to the caller.
    for url in _WEBHOOK_URLS:
        try:
            httpx.post(url, json=record, timeout=_WEBHOOK_TIMEOUT)
        except Exception:  # pragma: no cover - defensive
            pass


def _tier(kwargs) -> str | None:
    md = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    return (md.get("model_info") or {}).get("backend_tier")


def _latency_ms(slo) -> float | None:
    """Time-to-COMPLETION of the attempt, in ms — end minus start.

    Deliberately computed from the raw `startTime`/`endTime` timestamps rather
    than LiteLLM's `response_time`, because on the pinned v1.83.14-stable
    `response_time` is NOT time-to-completion for a STREAMED call: LiteLLM's
    `StandardLoggingPayloadSetup.get_response_time` returns
    `completionStartTime - startTime` (i.e. TTFT) when `stream=True`, and only
    `endTime - startTime` otherwise. Sourcing latency straight from the
    timestamps keeps latency_ms meaning time-to-completion for BOTH streamed and
    non-streamed attempts, so the ttft_ms below is a true subset of it. Falls
    back to `response_time` only when the timestamps are absent (e.g. a
    non-proxy code path), preserving the pre-goal-18 value there."""
    start = slo.get("startTime")
    end = slo.get("endTime")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        return round((end - start) * 1000, 1)
    rt = slo.get("response_time")
    return round(rt * 1000, 1) if isinstance(rt, (int, float)) else None


def _ttft_ms(slo) -> float | None:
    """Time-to-first-token, in ms — the FELT latency of a STREAMED response
    (goal 18). Measured from LiteLLM's own timestamps: `completionStartTime`
    minus `startTime`.

    LiteLLM stamps `completionStartTime` at the moment the first token arrives
    (`Logging._update_completion_start_time`, called from the streaming wrapper),
    and marks the payload `stream: True` once the streamed response is complete.
    For a NON-streamed call `completionStartTime` defaults to `endTime`, so TTFT
    would just equal latency and carry no signal — hence we return None unless
    the payload is explicitly `stream: True`, and non-streamed records OMIT
    ttft_ms entirely.

    Verified against the pinned litellm==1.83.14: StandardLoggingPayload carries
    {startTime, completionStartTime, stream} (litellm/types/utils.py) and the
    streaming path populates completionStartTime with the first-chunk time. See
    docs/09. Clamped at >= 0 so any sub-ms clock jitter can't yield a spurious
    negative; by construction startTime <= completionStartTime <= endTime, so a
    real value is always <= latency_ms."""
    if not slo.get("stream"):
        return None
    start = slo.get("startTime")
    first = slo.get("completionStartTime")
    if isinstance(start, (int, float)) and isinstance(first, (int, float)):
        return max(round((first - start) * 1000, 1), 0.0)
    return None


def _identity(user_api_key_dict) -> dict:
    """The synthetic identity of the CALLER, read off LiteLLM's UserAPIKeyAuth
    (the `user_api_key_dict` the success hook receives and, until goal 15, threw
    away). Answers *who* asked — the alias of the virtual key, and the user/team
    it was minted for (goal 11b's key->user->team binding).

    All three are None when the MASTER KEY or NO key store is in play: the master
    key carries no alias/user/team, and the bare-pytest + cli-auth profiles
    authenticate with the master key. So those profiles keep working and simply
    carry a null identity — never a crash, never a bogus id. Any attribute-read
    hiccup also degrades to None (identity must never break the request path)."""

    def _get(attr):
        try:
            v = getattr(user_api_key_dict, attr, None)
        except Exception:  # pragma: no cover - defensive
            return None
        # Treat empty string / "default" sentinels the same as absent so a
        # rollup groups them under "no key" rather than a phantom identity.
        return v if v not in ("", None) else None

    return {
        "key_alias": _get("key_alias"),
        "user_id": _get("user_id"),
        "team_id": _get("team_id"),
    }


def _complexity(messages, tools):
    """SHADOW complexity signal (goal 21) — a deterministic, fully-auditable
    classification of how "hard" a request looks, stamped on routing records so
    the future task-aware router gets designed against REAL request
    distributions instead of guesses (GOALS.md: the parked routing-granularity
    decision).

    Inspired by Fugu/TRINITY (Sakana AI): their core routing lever is a
    per-request complexity gate in front of a heterogeneous model pool. Two
    deliberate ANTI-Fugu constraints (their routing is proprietary and opaque):
      * DETERMINISTIC + TRANSPARENT — a documented decision tree over request
        features only (no model call, no scoring net), and the full feature
        vector rides on the record so every classification can be audited
        after the fact.
      * SHADOW ONLY — computed inside the LOGGING hooks, after routing is
        decided. It influences NOTHING: no routing, no latency on the request
        path. Repeat: this is telemetry, not policy.

    The decision tree (in precedence order):
      * agentic — tools are offered AND the conversation shows an agent loop
                  in motion: a tool/function-role message or an assistant
                  message carrying tool_calls, or >2 turns with tools.
      * toolful — tools are offered, single-shot (no loop evidence yet).
      * heavy   — no tools, but a big prompt (approx_prompt_tokens > 2000)
                  or a long transcript (> 4 turns).
      * trivial — everything else: the short tool-less ask.

    approx_prompt_tokens is chars/4 over message content (string or list-part
    text) PLUS the serialized tool schemas — tools are injected into the real
    prompt, so they count toward its weight. A crude proxy on purpose: stable,
    dependency-free, good enough to bucket by; the exact token count already
    rides on the records (goal 3/20).

    Returns None when messages are absent/unreadable — the record then simply
    OMITS the tag (never a crash, never a guess)."""
    if not isinstance(messages, list) or not messages:
        return None
    tools_n = len(tools) if isinstance(tools, list) else 0
    turns = len(messages)
    chars = 0
    loop_evidence = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") in ("tool", "function"):
            loop_evidence = True
        if isinstance(m.get("tool_calls"), list) and m.get("tool_calls"):
            loop_evidence = True
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chars += len(part["text"])
    if tools_n:
        try:
            chars += len(json.dumps(tools, default=str))
        except Exception:  # pragma: no cover - defensive
            pass
    approx_tokens = max(1, chars // 4)
    if tools_n and (loop_evidence or turns > 2):
        bucket = "agentic"
    elif tools_n:
        bucket = "toolful"
    elif approx_tokens > 2000 or turns > 4:
        bucket = "heavy"
    else:
        bucket = "trivial"
    return {
        "bucket": bucket,
        "approx_prompt_tokens": approx_tokens,
        "turns": turns,
        "tools": tools_n,
    }


# The LiteLLM-native session carrier (goal 22). VERIFIED on the pinned
# v1.83.14 (probed live, not guessed): the raw inbound header map reaches BOTH
# logging surfaces — the delivered hook at data["metadata"]["headers"] and the
# attempt events at kwargs["litellm_params"]["metadata"]["headers"] (streamed
# included) — while LiteLLM's own request_tags parsing does NOT pick this
# header up on this pin (it only derives User-Agent tags). So we read the raw
# header ourselves. Auth headers are already stripped from that map by LiteLLM;
# we additionally read ONLY this one key and never emit the header map.
_SESSION_HEADER = "x-litellm-tags"
_SESSION_TAG_PREFIX = "session:"
# The STUB escalation trigger (goal 25): a bare `escalate` entry in the same
# header fires docs/12 §5's state machine in shadow. Client-signaled on
# purpose — it proves the mechanics without pre-deciding the real trigger
# (still § Needs-a-human, decided against accumulated telemetry).
_ESCALATE_TAG = "escalate"


def _tags(headers):
    """The x-litellm-tags header parsed to a list of non-empty entries.
    Empty on anything unreadable — never a crash."""
    raw = headers.get(_SESSION_HEADER) if isinstance(headers, dict) else None
    if not isinstance(raw, str):
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _session(headers, messages):
    """SHADOW session classification (goal 22) — is this request a turn of a
    stateful conversation or a stateless one-shot, and what key would a sticky
    router pin it on? The decided HYBRID granularity (docs/03 decision block)
    routes those two shapes differently; this tag PROVES the classification is
    possible at the proxy, as telemetry, before any routing policy consumes it.
    Same discipline as goal 21: deterministic, documented, computed in the
    logging hooks AFTER routing — zero influence, zero request-path latency.

    request_class — transcript shape, per request (NOT a session tracker):
      * session-turn — the transcript shows a conversation in progress: any
                       assistant/tool/function-role message. A coding agent's
                       turn 2+ always matches (the client replays the growing
                       transcript).
      * one-shot    — no prior conversational state. NOTE the honest edge: the
                       FIRST turn of a real session also looks like this — the
                       proxy cannot see the future. The explicit session tag
                       (below) is what disambiguates turn 1; that asymmetry is
                       exactly the telemetry this goal exists to expose.

    stickiness_key — precedence, first hit wins:
      1. tag        — the client declared a session: a `session:<id>` entry in
                      the x-litellm-tags header (comma-separated; Codex can
                      carry its native session_id here, Claude Code injects it
                      via ANTHROPIC_CUSTOM_HEADERS — goal 17, no client
                      patching). Trusted from turn 1, one-shots included.
      2. transcript — session-turns only: sha256 of the FIRST user turn's
                      content, truncated. Agent transcripts grow append-only,
                      so the first user turn is constant across a session —
                      a stable key with zero client cooperation. Documented
                      limitation: two sessions opening with byte-identical
                      first prompts collide; the tag path is the fix.
      3. null       — an untagged one-shot needs no stickiness.

    Returns {request_class, stickiness_key, key_source} or None when messages
    are absent/unreadable (the record then omits the tag — never a guess)."""
    if not isinstance(messages, list) or not messages:
        return None
    has_prior = any(
        isinstance(m, dict) and m.get("role") in ("assistant", "tool", "function")
        for m in messages
    )
    request_class = "session-turn" if has_prior else "one-shot"
    key = None
    source = None
    for tag in _tags(headers):
        if tag.startswith(_SESSION_TAG_PREFIX) and len(tag) > len(_SESSION_TAG_PREFIX):
            key = tag[len(_SESSION_TAG_PREFIX) :]
            source = "tag"
            break
    if key is None and request_class == "session-turn":
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content")
                if not isinstance(content, str):
                    try:
                        content = json.dumps(content, default=str)
                    except Exception:  # pragma: no cover - defensive
                        content = str(content)
                key = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[
                    :16
                ]
                source = "transcript"
                break
    return {
        "request_class": request_class,
        "stickiness_key": key,
        "key_source": source,
    }


# --- SHADOW routing policy — the stateless arm (goal 24) ---------------------
# docs/12 §4 as code, in shadow. _policy_stateless is a PURE function (offline
# fast-tier tests pin every filter + the order); everything around it is the
# plumbing that feeds it and carries its block onto the records.

# Tier cost order for step 4 — cheaper first. Unknown tiers sort LAST (a
# backend that never declared its tier can still be chosen, but never beats a
# declared one).
_TIER_RANK = {"local": 0, "foundry": 1}
# LiteLLM key-allowlist wildcards: a key carrying either grants all models, so
# governance must NOT treat them as a one-model allowlist.
_ALLOWLIST_WILDCARDS = ("all-proxy-models", "*")
# Complexity buckets that demand an agent_capable backend (docs/12 §4 step 2).
_AGENT_BUCKETS = ("toolful", "agentic")


def _policy_stateless(candidates, key_models, bucket, registry_models, registry_state):
    """The stateless cheapest-capable policy (docs/12 §4), applied VERBATIM in
    the spec's order — governance → agent_capable gate → health → cheapest
    tier, tie-break lowest in_flight. Pure + deterministic: same inputs, same
    block, and the reason names what each step did so every decision is
    auditable from the record alone (the anti-Fugu constraint).

    Inputs:
      candidates      — [{model, tier, agent_capable}] from the gateway CONFIG
                        (one entry per alias; see _config_candidates)
      key_models      — the calling key's model allowlist (UserAPIKeyAuth.models;
                        empty/None/wildcard ⇒ unrestricted)
      bucket          — the request's shadow complexity bucket (goal 21) or None
      registry_models — {model: aggregate} from the control-plane /models, or
                        None when degraded to config-only
      registry_state  — "live" | "absent" | "stale" (how registry_models came
                        to be; stamped on the block so degrade is on-record)

    Returns the shadow policy block with actual/agree left None — they are
    filled in when reality (the served backend) is known, post-response."""
    steps = []
    pool = [dict(c) for c in candidates if isinstance(c, dict) and c.get("model")]

    # 1. governance — the key's allowlist bounds where this caller may EVER
    # route (the "never leaves the building" rule, docs/12 §1).
    allow = None
    if isinstance(key_models, list):
        names = [m for m in key_models if isinstance(m, str) and m]
        if names and not any(w in names for w in _ALLOWLIST_WILDCARDS):
            allow = set(names)
    if allow is not None:
        before = len(pool)
        pool = [c for c in pool if c["model"] in allow]
        steps.append("governance key-allowlist %d->%d" % (before, len(pool)))
    else:
        steps.append("governance: key unrestricted")

    # 2. agent_capable gate — only toolful/agentic buckets demand it. The
    # verdict comes from the registry when the model is registered (any healthy
    # instance capable), else the config declaration (the conformance gate's
    # declared-by-config story for mocks, earned for real models).
    if bucket in _AGENT_BUCKETS:
        before = len(pool)

        def _capable(c):
            reg = (registry_models or {}).get(c["model"])
            if isinstance(reg, dict) and "agent_capable" in reg:
                return bool(reg.get("agent_capable"))
            return bool(c.get("agent_capable"))

        pool = [c for c in pool if _capable(c)]
        steps.append(
            "agent_capable gate (bucket=%s) %d->%d" % (bucket, before, len(pool))
        )
    else:
        steps.append("agent_capable gate not applied (bucket=%s)" % bucket)

    # 3. health — control-plane derived (docs/10 D3: reported-healthy AND
    # heartbeat-fresh). Applies only to models the registry KNOWS: workbenches
    # heartbeat, Foundry backends don't, so an unregistered model passes on
    # config rather than being exiled for never having heartbeat. When the
    # registry is absent/stale this step degrades to a no-op — config-only
    # candidates — and the block's `registry` field says so.
    if registry_models is not None:
        before = len(pool)

        def _healthy(c):
            reg = registry_models.get(c["model"])
            if isinstance(reg, dict) and "healthy" in reg:
                return (reg.get("healthy") or 0) >= 1
            return True  # unregistered ⇒ config-declared, no live signal

        pool = [c for c in pool if _healthy(c)]
        steps.append("health via control-plane %d->%d" % (before, len(pool)))
    else:
        steps.append("health degraded to config-only (registry %s)" % registry_state)

    # 4. cheapest capable — cheaper tier first, tie-break lowest in_flight
    # (control-plane; unregistered models carry no load signal and count 0),
    # then name so the order is total and the choice deterministic.
    def _in_flight(c):
        reg = (registry_models or {}).get(c["model"])
        v = reg.get("in_flight") if isinstance(reg, dict) else None
        return v if isinstance(v, (int, float)) else 0

    pool.sort(
        key=lambda c: (
            _TIER_RANK.get(c.get("tier"), len(_TIER_RANK)),
            _in_flight(c),
            c["model"],
        )
    )
    chosen = pool[0] if pool else None
    if chosen is not None:
        steps.append(
            "chose %s (tier=%s, in_flight=%d)"
            % (chosen["model"], chosen.get("tier"), _in_flight(chosen))
        )
    else:
        steps.append("no capable candidate survived")
    return {
        "arm": "stateless",
        "candidate_set": [c["model"] for c in pool],
        "chosen": chosen["model"] if chosen else None,
        "reason": "; ".join(steps),
        # How the health signal was sourced — "live" or the degrade mode
        # ("absent"/"stale" ⇒ config-only candidates). The completion
        # condition's "the record says so".
        "registry": registry_state,
        # Filled post-response, when reality is known (see the hooks).
        "actual": None,
        "agree": None,
    }


def _config_candidates() -> list:
    """The candidate universe: every alias the gateway is CONFIGURED to serve,
    with its tier + agent_capable declaration, read from the proxy's live
    router (the same model_list the config file fed it). Deduped by alias —
    multiple deployments of one alias collapse into a single candidate that is
    agent_capable/tiered if ANY deployment declares it. Empty on any hiccup
    (no proxy, no router yet): the policy then has nothing to say and the
    record simply omits the block — never a crash on the request path."""
    try:
        from litellm.proxy import proxy_server

        deployments = (
            proxy_server.llm_router and proxy_server.llm_router.model_list
        ) or []
    except Exception:  # pragma: no cover - defensive
        return []
    out: dict = {}
    for d in deployments:
        if not isinstance(d, dict):
            continue
        name = d.get("model_name")
        if not name:
            continue
        info = d.get("model_info") or {}
        entry = out.setdefault(
            name, {"model": name, "tier": None, "agent_capable": False}
        )
        if entry["tier"] is None and info.get("backend_tier"):
            entry["tier"] = info.get("backend_tier")
        if info.get("agent_capable"):
            entry["agent_capable"] = True
    return list(out.values())


# --- SHADOW sticky pins + escalation mechanics — the session arm (goal 25) ---
# docs/12 §2 (decision table), §3 (sticky sessions) and §5 (escalation, one hop
# upward only) as code, still in shadow. _policy_session is sync + clock-
# injected so the offline fast tier can pin the whole state machine (TTL
# expiry, restart, exactly-once) without docker or a real clock.

# Pin TTL — inactivity-based (docs/12 §3): a pin a session keeps touching
# never expires; an abandoned one ages out and the next turn re-pins. The
# spec's suggested default: 24h, config knob.
_PIN_TTL_S = float(os.environ.get("POLICY_PIN_TTL_S", "86400"))
_PIN_CAP = 4096
# Where the pins live INSIDE the gateway container. Container-scoped tmp by
# default: shared by every proxy worker, gone with the container.
_PIN_DB = os.environ.get(
    "POLICY_PIN_DB", os.path.join(tempfile.gettempdir(), "shadow_pins.db")
)


class _PinStore:
    """Gateway-local shadow pin store — docs/12 §3 option (a), the decided
    default for the single-gateway build phase (Postgres promotion is a later,
    flagged decision — docs/12 §8.3). Keyed by goal-22 stickiness_key; a pin
    is {backend, tier, escalated, pinned_at, last_seen}.

    Backing: a CONTAINER-SCOPED SQLite file (POLICY_PIN_DB, default under
    /tmp), NOT process memory — discovered the hard way: every profile runs
    the proxy with --num_workers 2, and pins are the first CROSS-REQUEST
    state, so per-process memory made same-session requests flap between
    workers' independent stores (a multi-worker gateway is already "replicas"
    in docs/12 §3(a)'s sense). The file keeps §3(a)'s intent — nothing leaves
    the gateway container, no shared infra, no schema in the shared Postgres
    (that promotion is the flagged replica-time decision) — while being one
    store for all workers. Same pattern as the control-plane's SQLite. Writes
    are guarded SQL, so pin-once and escalate-once hold ATOMICALLY across
    workers, not just threads.

    Restart story, BY DESIGN: a recreated container starts with a fresh /tmp,
    so a restart loses every pin. That is safe — an unpinned session-turn
    just re-pins on its next request (docs/12 §3: "the cache-loss cost is the
    same as a restart today"), and the exactly-once escalation guarantee is
    per-PIN, not per-session-eternal: a re-pinned session gets its hop back,
    the honest reading of losing the state. Bounded (least-recently-seen
    eviction past `cap`) so a tag-spraying client can't grow the store
    without bound. Timestamps are caller-injected (time.monotonic() in the
    hook — one kernel clock, consistent across workers; a fake clock in the
    offline tests). Lock per process, busy_timeout across them."""

    def __init__(self, ttl_s=None, cap=_PIN_CAP, path=None):
        self.ttl_s = _PIN_TTL_S if ttl_s is None else float(ttl_s)
        self.cap = cap
        self.path = _PIN_DB if path is None else path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=2000")
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS pins ("
                " key TEXT PRIMARY KEY, backend TEXT, tier TEXT,"
                " escalated INTEGER NOT NULL DEFAULT 0,"
                " pinned_at REAL, last_seen REAL)"
            )

    @staticmethod
    def _pin(row):
        return {
            "backend": row[0],
            "tier": row[1],
            "escalated": bool(row[2]),
            "pinned_at": row[3],
            "last_seen": row[4],
        }

    _SELECT = (
        "SELECT backend, tier, escalated, pinned_at, last_seen FROM pins WHERE key=?"
    )

    def get(self, key, now):
        """The live pin for `key`, or None. An expired pin is deleted and
        reported absent — expiry is not an error, the caller re-pins."""
        with self._lock, self._conn:
            row = self._conn.execute(self._SELECT, (key,)).fetchone()
            if row is None:
                return None
            if now - row[4] > self.ttl_s:
                self._conn.execute("DELETE FROM pins WHERE key=?", (key,))
                return None
            return self._pin(row)

    def pin(self, key, backend, tier, now):
        """Record a FIRST-SIGHT pin. First writer wins across workers
        (INSERT OR IGNORE): returns (the store's pin — ours or the concurrent
        winner's, read back — , created?). An expired leftover never blocks a
        fresh pin."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM pins WHERE key=? AND ? - last_seen > ?",
                (key, now, self.ttl_s),
            )
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO pins"
                " (key, backend, tier, escalated, pinned_at, last_seen)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (key, backend, tier, now, now),
            )
            created = cur.rowcount == 1
            if created:
                # Bound the store — evict least-recently-seen overflow (the
                # just-inserted row has the newest last_seen, so it survives).
                self._conn.execute(
                    "DELETE FROM pins WHERE key NOT IN"
                    " (SELECT key FROM pins ORDER BY last_seen DESC LIMIT ?)",
                    (self.cap,),
                )
            row = self._conn.execute(self._SELECT, (key,)).fetchone()
            return (self._pin(row) if row else None, created)

    def escalate(self, key, backend, tier, now):
        """The upward flip, exactly once ATOMICALLY: only a not-yet-escalated
        pin moves (guarded UPDATE), so two workers firing simultaneously
        cannot both burn the hop. True iff THIS call flipped it."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE pins SET backend=?, tier=?, escalated=1, last_seen=?"
                " WHERE key=? AND escalated=0",
                (backend, tier, now, key),
            )
            return cur.rowcount == 1

    def touch(self, key, now):
        """Refresh the inactivity TTL on a pin hit."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE pins SET last_seen=? WHERE key=?", (now, key))


# Created lazily so importing the module (offline tests, tooling) does not
# touch the filesystem — only the hook's first session-arm request does.
_PINS_INSTANCE: _PinStore | None = None


def _pins():
    global _PINS_INSTANCE
    if _PINS_INSTANCE is None:
        _PINS_INSTANCE = _PinStore()
    return _PINS_INSTANCE


def _tier_of(candidates, model):
    for c in candidates:
        if isinstance(c, dict) and c.get("model") == model:
            return c.get("tier")
    return None


def _policy_session(
    pins,
    key,
    escalate,
    candidates,
    key_models,
    bucket,
    registry_models,
    registry_state,
    now,
):
    """The session arm (docs/12 §2 rows 2–5 + §5's state machine), in shadow.

    * pin MISS — route as if new: the stateless arm (docs/12 §4, all four
      filters) picks, and its choice becomes the key's pin. This covers both
      turn 1 of a declared session and an unpinned session-turn (gateway
      restarted / TTL-expired / heuristic key).
    * pin HIT — the pinned backend, always: stickiness bypasses re-evaluation
      by design (no registry read, `registry: null` on the block — no health
      signal was consulted; docs/12 §6's down-backend interplay is the
      availability-fallback layer's job, not the pin's).
    * ESCALATE signal (the stub trigger) — upward only, exactly once:
      the target is the stateless arm re-run over the KNOWN tiers strictly
      above the pin's (governance/agent-gate/health still apply), the pin is
      REPLACED with it and marked escalated, and the request that fired the
      flip carries `escalated_from`. No downward edge exists. A second signal
      is a recorded no-op ("already escalated"). A signal that finds no
      capable higher-tier candidate is ALSO a recorded no-op that does NOT
      burn the hop — nothing moved, and a blip must not spend the session's
      one escalation (the §6 blip-must-not-burn-the-hop spirit).

    Pure over (pins-state, args): same store state + same inputs ⇒ same block
    and same store mutation. `now` is injected so offline tests drive the TTL
    with a fake clock. Returns the shadow block; actual/agree are filled
    post-response like the stateless arm's."""
    steps = []
    used_registry = None
    escalated_from = None
    pin = pins.get(key, now)
    pin_hit = pin is not None
    if pin is None:
        base = _policy_stateless(
            candidates, key_models, bucket, registry_models, registry_state
        )
        used_registry = registry_state
        if base["chosen"] is None:
            steps.append("pin miss: no capable candidate to pin [%s]" % base["reason"])
        else:
            tier = _tier_of(candidates, base["chosen"])
            pin, created = pins.pin(key, base["chosen"], tier, now)
            if created:
                steps.append(
                    "pin miss: pinned %s (tier=%s) via stateless arm [%s]"
                    % (base["chosen"], tier, base["reason"])
                )
            elif pin is not None:
                # Lost a same-key race to another worker/request — the
                # store's pin is the session's truth, ours is discarded.
                pin_hit = True
                steps.append(
                    "pin miss -> concurrent pin won: %s (tier=%s)"
                    % (pin["backend"], pin["tier"])
                )
            else:  # pragma: no cover - insert-then-vanish needs a 3-way race
                steps.append("pin miss -> concurrent pin vanished")
    else:
        pins.touch(key, now)
        steps.append(
            "pin hit: %s (tier=%s, escalated=%s)"
            % (pin["backend"], pin["tier"], pin["escalated"])
        )
    if escalate:
        if pin is None:
            steps.append("escalate signal: no-op (nothing pinned to escalate)")
        elif pin["escalated"]:
            steps.append(
                "escalate signal: no-op (already escalated — one hop per session, ever)"
            )
        else:
            floor = _TIER_RANK.get(pin["tier"])
            if floor is None:
                # An undeclared tier has no place in the upward order — there
                # is nothing provably "higher" to move to.
                steps.append(
                    "escalate signal: no-op (pinned tier undeclared — no upward order)"
                )
            else:
                upward = [
                    c
                    for c in candidates
                    if isinstance(c, dict)
                    and _TIER_RANK.get(c.get("tier")) is not None
                    and _TIER_RANK[c["tier"]] > floor
                ]
                base = _policy_stateless(
                    upward, key_models, bucket, registry_models, registry_state
                )
                used_registry = registry_state
                if base["chosen"]:
                    tier = _tier_of(candidates, base["chosen"])
                    if pins.escalate(key, base["chosen"], tier, now):
                        escalated_from = pin["backend"]
                        pin = {
                            "backend": base["chosen"],
                            "tier": tier,
                            "escalated": True,
                        }
                        steps.append(
                            "escalate signal: pin %s -> %s (upward, exactly"
                            " once) [%s]"
                            % (escalated_from, base["chosen"], base["reason"])
                        )
                    else:
                        # The guarded UPDATE found no un-escalated pin: a
                        # concurrent signal flipped it first (or the pin was
                        # dropped). Exactly-once held at the store — this
                        # request records the no-op, not a second hop.
                        pin = pins.get(key, now) or pin
                        steps.append(
                            "escalate signal: no-op (concurrent escalation"
                            " already flipped the pin)"
                        )
                else:
                    steps.append(
                        "escalate signal: no-op, hop NOT burned (no capable"
                        " higher-tier candidate: %s)" % base["reason"]
                    )
    block = {
        "arm": "session",
        # The pin key on the block itself so a record is auditable alone
        # (the session tag block carries it too — deliberate redundancy).
        "stickiness_key": key,
        "pin_hit": pin_hit,
        "pinned_backend": pin["backend"] if pin else None,
        "escalated": bool(pin["escalated"]) if pin else False,
        "chosen": pin["backend"] if pin else None,
        "reason": "; ".join(steps),
        # null on a pure pin hit: no candidate evaluation ran, so no health
        # signal was sourced — stamping "live" would be a lie.
        "registry": used_registry,
        "actual": None,
        "agree": None,
    }
    if escalated_from is not None:
        block["escalated_from"] = escalated_from
    return block


def _request_headers(data):
    """The inbound header map at PRE-CALL time. LiteLLM stamps it into the
    request metadata before pre-call hooks run, but the metadata key varies
    across the three inbound protocols on this pin ("metadata" for
    chat/completions, "litellm_metadata" elsewhere) — check both, then fall
    back to the raw proxy_server_request map (stamped at the same point; may
    still carry auth headers, which is fine because callers read ONLY the
    tags key and never emit the map). None when absent: the request then
    simply has no tag-derived session signal."""
    if not isinstance(data, dict):
        return None
    for mk in ("metadata", "litellm_metadata"):
        md = data.get(mk)
        if isinstance(md, dict) and isinstance(md.get("headers"), dict):
            return md["headers"]
    psr = data.get("proxy_server_request")
    if isinstance(psr, dict) and isinstance(psr.get("headers"), dict):
        return psr["headers"]
    return None


# --- ENFORCEMENT — the policy drives routing, behind a flag (goal 26) --------
# ROUTER_POLICY=shadow (the default: everything above stays pure telemetry,
# byte-for-byte the pre-goal-26 behavior) | enforce (the owned pre-call hook
# REWRITES the requested model to the policy's choice — docs/12 R1, verified
# live on the pin in the goal-26 pre-build research, docs/12 §7 addendum).
_ROUTER_POLICY = os.environ.get("ROUTER_POLICY", "shadow").strip().lower()


def _apply_enforcement(block, data):
    """Make the policy decision REAL: point data["model"] at the block's
    chosen backend, for both arms (the session arm's chosen is the pin, the
    escalated pin included). Mutates block + data in place.

    Two research-mandated moves (docs/12 §7 goal-26 addendum):
      * STASH THE ORIGINAL ASK FIRST — post-rewrite, nothing downstream can
        reconstruct it (router, logging and records all see only the new
        model; the client's own response.model is restored to the original on
        the direct path). block["requested"] is the only durable carrier, so
        records get the full requested vs chosen vs served triple.
      * The policy's governance filter is the SOLE allowlist guard for the
        rewrite target — LiteLLM checks the key's models only at auth time,
        against the REQUESTED model, and never re-checks. The block's chosen
        is always drawn from allowlist-filtered candidates (docs/12 §4 step
        1), which is exactly why this function never needs its own check —
        pinned by the dedicated governance e2e test.

    A block with no survivor (chosen None) rewrites nothing: the request
    proceeds on the client's own ask — enforcement degrades to shadow for
    that request, never to a failure. After the rewrite, LiteLLM's
    availability-fallback applies to the CHOSEN model's chain (R4, verified),
    so `actual` may still differ from `chosen` — that is the chain firing,
    visible as agree:false with fallback:true on the record."""
    requested = data.get("model") if isinstance(data, dict) else None
    block["enforced"] = True
    block["requested"] = requested
    chosen = block.get("chosen")
    if chosen and requested and chosen != requested:
        data["model"] = chosen
    return block


# Control-plane registry access for step 3 — TTL-cached so the pre-call cost is
# one bounded HTTP read per cache window, not per request. The e2e stack sets
# POLICY_REGISTRY_CACHE_S=0 so every request sees the test's freshest
# heartbeats (mockd-speed, determinism over amortization there).
_REGISTRY_URL = os.environ.get("CONTROL_PLANE_URL", "").rstrip("/")
_REGISTRY_CACHE_S = float(os.environ.get("POLICY_REGISTRY_CACHE_S", "2.0"))
_REGISTRY_STALE_S = float(os.environ.get("POLICY_REGISTRY_STALE_S", "10.0"))
_REGISTRY_TIMEOUT_S = float(os.environ.get("POLICY_REGISTRY_TIMEOUT_S", "0.5"))
_REGISTRY_CACHE = {"at": 0.0, "models": None}  # monotonic time of last SUCCESS


async def _registry_snapshot():
    """(registry_models, state) for the policy's health step. state is "live"
    (fresh data — from cache within TTL, a fetch, or riding a recent snapshot
    through a blip), "stale" (unreachable and the last snapshot outlived
    POLICY_REGISTRY_STALE_S) or "absent" (no URL configured / unreachable with
    nothing cached). Degraded states return models=None ⇒ config-only.
    Async + short-timeout so a hung control-plane costs at most
    POLICY_REGISTRY_TIMEOUT_S of pre-call latency per cache window, and never
    wedges the event loop."""
    if not _REGISTRY_URL or httpx is None:
        return None, "absent"
    now = time.monotonic()
    age = now - _REGISTRY_CACHE["at"]
    if _REGISTRY_CACHE["models"] is not None and age <= _REGISTRY_CACHE_S:
        return _REGISTRY_CACHE["models"], "live"
    try:
        async with httpx.AsyncClient(timeout=_REGISTRY_TIMEOUT_S) as client:
            resp = await client.get(_REGISTRY_URL + "/models")
            payload = resp.json()
        models = {
            m["model"]: m
            for m in (payload.get("models") or [])
            if isinstance(m, dict) and m.get("model")
        }
        _REGISTRY_CACHE["at"] = time.monotonic()
        _REGISTRY_CACHE["models"] = models
        return models, "live"
    except Exception:
        if _REGISTRY_CACHE["models"] is None:
            return None, "absent"
        if age <= _REGISTRY_STALE_S:
            # A blip, not an outage: the last good snapshot is recent enough to
            # still be a fair health signal — ride it.
            return _REGISTRY_CACHE["models"], "live"
        return None, "stale"


# The computed block, keyed by the goal-16 correlation id, so the delivered
# hook and the attempt events (which see the same id — that is goal 16's whole
# point) can stamp it onto their records. Bounded FIFO: entries are never
# popped on delivery (attempts log late), they just age out. Lock because the
# sync log_* variants may run off the event loop's thread.
_POLICY_BLOCKS: OrderedDict = OrderedDict()
_POLICY_BLOCKS_CAP = 4096
_POLICY_LOCK = threading.Lock()

# --- STREAMED delivered records (goal 29) ------------------------------------
# Request context stashed at PRE-CALL time, keyed by the goal-16 correlation
# id, so the streamed delivered-equivalent record (built in the success EVENT,
# where the proxy's `data` dict is out of reach) can carry what only ingress
# knows: the model the client's request was routed FOR (post-enforcement,
# matching the non-streamed delivered record's requested_model semantics), the
# caller's identity read off UserAPIKeyAuth (byte-identical semantics to the
# non-streamed path — no reliance on litellm's metadata sentinels), and the
# session tag derived via _request_headers (which reads ALL THREE inbound
# protocols' metadata shapes; the event-time header map is chat-only on this
# pin). Same bounded-FIFO discipline as _POLICY_BLOCKS, same lock (both are
# tiny critical sections touched by the same hooks). Entries age out rather
# than being popped: attempts may log after delivery.
_REQUEST_CTX: OrderedDict = OrderedDict()
_REQUEST_CTX_CAP = 4096

# Correlation ids a delivered(-equivalent) record was already emitted for —
# the double-emission guard. On the pinned v1.83.14 the two carriers are
# mutually exclusive by construction (async_post_call_success_hook never runs
# for streams, success events carry stream:true only for streams), so this is
# pure insurance: if a litellm upgrade ever fires both for one request, the
# dashboard must not grow duplicate request rows. Bounded like the maps above.
_DELIVERED_CIDS: OrderedDict = OrderedDict()
_DELIVERED_CIDS_CAP = 4096


def _ctx_remember(cid, ctx) -> None:
    if not cid or not isinstance(ctx, dict):
        return
    with _POLICY_LOCK:
        _REQUEST_CTX[cid] = ctx
        while len(_REQUEST_CTX) > _REQUEST_CTX_CAP:
            _REQUEST_CTX.popitem(last=False)


def _ctx_recall(cid):
    if not cid:
        return None
    with _POLICY_LOCK:
        return _REQUEST_CTX.get(cid)


def _delivered_mark_once(cid) -> bool:
    """True iff no delivered record was emitted for `cid` yet — and mark it.
    A None cid cannot be deduped; let it through (never drop a record over a
    missing join key)."""
    if not cid:
        return True
    with _POLICY_LOCK:
        if cid in _DELIVERED_CIDS:
            return False
        _DELIVERED_CIDS[cid] = True
        while len(_DELIVERED_CIDS) > _DELIVERED_CIDS_CAP:
            _DELIVERED_CIDS.popitem(last=False)
        return True


def _policy_remember(cid, block) -> None:
    if not cid or not isinstance(block, dict):
        return
    with _POLICY_LOCK:
        _POLICY_BLOCKS[cid] = block
        while len(_POLICY_BLOCKS) > _POLICY_BLOCKS_CAP:
            _POLICY_BLOCKS.popitem(last=False)


def _policy_recall(cid):
    if not cid:
        return None
    with _POLICY_LOCK:
        return _POLICY_BLOCKS.get(cid)


def _policy_with_outcome(block, actual):
    """The block plus reality: actual = the backend that (this record says)
    served, agree = chosen == actual. None-safe on both sides — a block with no
    survivor (chosen None) or a record with no served backend yields
    agree: null, never a fake verdict."""
    b = dict(block)
    b["actual"] = actual
    b["agree"] = (b.get("chosen") == actual) if (b.get("chosen") and actual) else None
    return b


def _llm_call_record(kwargs, fallback_status: str) -> dict:
    """Build an `llm_call` record from a success/failure event's kwargs."""
    slo = kwargs.get("standard_logging_object") or {}
    err = slo.get("error_information") or {}
    record = {
        "event": "llm_call",
        "status": slo.get("status") or fallback_status,
        # The alias the client asked for (the router "model group").
        "requested_group": slo.get("model_group"),
        # The concrete backend/deployment that this attempt hit.
        "backend": slo.get("model"),
        "backend_model_id": (slo.get("model_id") or "")[:12] or None,
        "api_base": slo.get("api_base"),
        "tier": _tier(kwargs),
        # Time-to-completion of the attempt (see _latency_ms — sourced from raw
        # timestamps so it stays completion-time even for streamed calls).
        "latency_ms": _latency_ms(slo),
        "tokens": {
            "prompt": slo.get("prompt_tokens"),
            "completion": slo.get("completion_tokens"),
            "total": slo.get("total_tokens"),
        },
        # The "why" behind a fallback: present only on failed attempts.
        "error_code": err.get("error_code"),
        "error_class": err.get("error_class"),
        "litellm_call_id": slo.get("litellm_call_id"),
        "trace_id": slo.get("trace_id"),
        # The JOIN KEY (goal 16): the request-scoped trace_id, shared by every
        # attempt in a fallback group. The dashboard nests these under the
        # matching `delivered` request by this id.
        "correlation_id": slo.get("trace_id"),
    }
    # TTFT (goal 18): the felt latency of a STREAMED response — present ONLY on
    # streamed attempts. Non-streamed records omit the key entirely (for them
    # first-token == completion, so it would carry no signal). By construction
    # ttft_ms <= latency_ms. See _ttft_ms + docs/09.
    ttft = _ttft_ms(slo)
    if ttft is not None:
        record["ttft_ms"] = ttft
    # SHADOW complexity (goal 21) — best-effort on the attempt trail: the raw
    # messages/tools ride the logging kwargs on this pinned litellm; when absent
    # the tag is simply omitted. Attempt-level stamping keeps every attempt
    # auditable alone, and covered streamed traffic until goal 29 gave streams
    # a delivered record of their own.
    cx = _complexity(
        kwargs.get("messages") or slo.get("messages"),
        (kwargs.get("optional_params") or {}).get("tools"),
    )
    if cx is not None:
        record["complexity"] = cx
    # SHADOW session classification (goal 22) — attempt-side stamping for the
    # same reason as complexity (per-attempt auditability). Headers verified
    # to reach litellm_params.metadata.headers on v1.83.14 (see _session).
    sess = _session(
        ((kwargs.get("litellm_params") or {}).get("metadata") or {}).get("headers"),
        kwargs.get("messages") or slo.get("messages"),
    )
    if sess is not None:
        record["session"] = sess
    # SHADOW policy (goal 24) — attempt-side stamping, best-effort, for the
    # same reason as complexity/session (per-attempt auditability; the
    # authoritative verdict is the delivered record's — streams included since
    # goal 29). actual = this attempt's backend on success; a FAILED attempt
    # served nothing, so it carries the decision with actual/agree null.
    block = _policy_recall(slo.get("trace_id"))
    if block is not None:
        actual = slo.get("model_group") if slo.get("status") == "success" else None
        record["shadow_policy"] = _policy_with_outcome(block, actual)
    return record


def _delivered_stream_record(kwargs):
    """A `delivered` record for a STREAMED response (goal 29), built from the
    post-stream success event — or None when this event is not one (not
    streamed / not a success / already delivered).

    WHY THIS HOOK (the research the goal demanded, verified live on the pinned
    v1.83.14 — see docs/09): async_post_call_success_hook structurally never
    runs for streams (every streaming route early-returns through an SSE
    generator before the proxy calls it), and the per-chunk
    async_post_call_streaming_hook carries neither the request dict nor a join
    key. But CustomStreamWrapper fires the SUCCESS EVENT when the client's
    stream is exhausted, with the full StandardLoggingPayload — stream:true,
    the assembled token usage, cost, api_base — and, crucially, it fires for
    the FALLBACK WINNER too, carrying the SAME pre-call trace_id (probed on
    all three inbound surfaces: chat, Anthropic-messages, Responses-bridge).
    The old "fallback winner fires no success event" quirk is a NON-streamed
    behavior. So the logging hook is a complete carrier for every stream a
    client actually finished — and it keeps this record OFF the request path
    (the iterator hook was rejected for exactly that reason: a bug there
    breaks live streams; a bug here loses a record).

    What the event alone cannot say is what the client was routed FOR: the
    winner's model_group IS the winner (a fallback would look direct). The
    pre-call context map fills that in — requested_model stashed post-
    enforcement, so `fallback` keeps meaning "the availability chain fired",
    exactly the non-streamed semantics. On a context miss (cap eviction /
    stamping hiccup) requested_model degrades to the served group — fallback
    then reads false, the same honest degrade the non-streamed record has
    when data["model"] is absent.

    A stream the client ABORTED (or that died mid-stream) fires the failure
    event, not this — such traffic stays visible via its attempt trail and
    the dashboard's unattributed counts, deliberately: nothing was delivered."""
    slo = kwargs.get("standard_logging_object") or {}
    if not slo.get("stream") or slo.get("status") != "success":
        return None
    cid = slo.get("trace_id")
    if not _delivered_mark_once(cid):
        return None
    ctx = _ctx_recall(cid) or {}
    served = slo.get("model_group")
    requested = ctx.get("requested_model") or served
    record = {
        "event": "delivered",
        # Marks the delivered record as stream-sourced (goal 29) — non-streamed
        # records omit the key. Auditability: says WHICH carrier built it.
        "stream": True,
        "requested_model": requested,
        "served_model": served,
        "served_model_id": (slo.get("model_id") or "")[:12] or None,
        "api_base": slo.get("api_base"),
        "provider": slo.get("custom_llm_provider"),
        "response_cost": slo.get("response_cost"),
        "tokens": {
            "prompt": slo.get("prompt_tokens"),
            "completion": slo.get("completion_tokens"),
            "total": slo.get("total_tokens"),
        },
        "fallback": bool(requested and served and requested != served),
        "correlation_id": cid,
        "litellm_call_id": slo.get("litellm_call_id"),
    }
    # WHO asked (goal 15): the identity stashed pre-call off UserAPIKeyAuth —
    # the same source the non-streamed record reads, so master-key/no-key
    # traffic carries the same nulls (litellm's own metadata would leak
    # "default_user_id" sentinels here).
    identity = ctx.get("identity")
    record.update(
        identity
        if isinstance(identity, dict)
        else {"key_alias": None, "user_id": None, "team_id": None}
    )
    cx = _complexity(
        kwargs.get("messages") or slo.get("messages"),
        (kwargs.get("optional_params") or {}).get("tools"),
    )
    if cx is not None:
        record["complexity"] = cx
    # Session tag: prefer the pre-call stash (derived via _request_headers,
    # which reads all three protocols' metadata shapes) over an event-time
    # re-derivation (whose header map is chat-only on this pin).
    sess = ctx.get("session") or _session(
        ((kwargs.get("litellm_params") or {}).get("metadata") or {}).get("headers"),
        kwargs.get("messages") or slo.get("messages"),
    )
    if sess is not None:
        record["session"] = sess
    block = _policy_recall(cid)
    if block is not None:
        record["shadow_policy"] = _policy_with_outcome(block, served)
    return record


class RoutingRecorder(CustomLogger):
    # --- ingress: stamp the correlation id (goal 16) -----------------------
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Stamp a request-scoped correlation id onto `data` BEFORE routing.

        Sets `data["litellm_trace_id"]` (unless the client already supplied one,
        which we keep). The router shares this id across the whole fallback group
        via setdefault, so every `llm_call` attempt carries it as its trace_id;
        and the proxy threads this SAME `data` dict into async_post_call_success_hook,
        so the `delivered` record can read it straight back — giving a guaranteed
        shared join key across a request and ALL its attempts (docs/09, goal 16).

        Best-effort and never fatal: any hiccup degrades to leaving `data`
        untouched (LiteLLM then generates its own per-attempt trace_id, i.e. the
        pre-goal-16 behaviour) rather than breaking the request path."""
        try:
            if isinstance(data, dict) and not data.get(_CORRELATION_KEY):
                data[_CORRELATION_KEY] = "obs-" + uuid.uuid4().hex
        except Exception:  # pragma: no cover - defensive
            pass
        # SHADOW routing policy (goals 24+25): compute, PRE-CALL, what the
        # hybrid policy would choose — then do NOTHING with it except remember
        # it for the records (keyed by the correlation id above, which every
        # record of this request will carry). Arm dispatch per docs/12 §2: a
        # stickiness key (goal 22's derivation, read pre-call) selects the
        # session arm — pin store + stub-trigger escalation; keyless requests
        # take the stateless arm (goal 24, unchanged). data["model"] is never
        # touched, the stream is never buffered, and any error degrades to "no
        # block" — the request path is sacred.
        try:
            if isinstance(data, dict) and data.get(_CORRELATION_KEY):
                candidates = _config_candidates()
                if candidates:
                    cx = _complexity(data.get("messages"), data.get("tools"))
                    bucket = (cx or {}).get("bucket")
                    key_models = getattr(user_api_key_dict, "models", None)
                    headers = _request_headers(data)
                    sess = _session(headers, data.get("messages"))
                    key = (sess or {}).get("stickiness_key")
                    if key:
                        escalate = _ESCALATE_TAG in _tags(headers)
                        now = time.monotonic()
                        # Registry read only when the session arm will actually
                        # evaluate candidates (pin miss, or a live escalation):
                        # a pure pin hit consults no health signal, so it must
                        # not pay for one. The double get() is benign — the
                        # peek and _policy_session see the same `now`, and a
                        # same-key race between them just turns one miss into
                        # a hit (both would pin the same choice anyway).
                        pin = _pins().get(key, now)
                        needs_eval = pin is None or (escalate and not pin["escalated"])
                        if needs_eval:
                            registry_models, registry_state = await _registry_snapshot()
                        else:
                            registry_models, registry_state = None, None
                        block = _policy_session(
                            _pins(),
                            key,
                            escalate,
                            candidates,
                            key_models,
                            bucket,
                            registry_models,
                            registry_state,
                            now,
                        )
                    else:
                        registry_models, registry_state = await _registry_snapshot()
                        block = _policy_stateless(
                            candidates,
                            key_models,
                            bucket,
                            registry_models,
                            registry_state,
                        )
                    # ENFORCEMENT (goal 26): behind the flag, the decision
                    # stops being shadow — the hook rewrites the model to the
                    # policy's choice (original ask stashed on the block
                    # first). Under the default ROUTER_POLICY=shadow this
                    # branch never runs and data is untouched, as ever.
                    if _ROUTER_POLICY == "enforce":
                        _apply_enforcement(block, data)
                    _policy_remember(data[_CORRELATION_KEY], block)
        except Exception:  # pragma: no cover - defensive
            pass
        # STREAMED delivered records (goal 29): stash what only ingress knows —
        # keyed by the correlation id, read back post-stream by the success
        # event. requested_model is read AFTER the enforcement branch above so
        # it names the model the request was routed FOR (the non-streamed
        # record's exact semantics — its requested_model is data["model"] read
        # post-rewrite; the client's original ask lives on the policy block).
        # Unconditional (unlike the policy block, which needs candidates): a
        # bare gateway with no router still delivers streams.
        try:
            if isinstance(data, dict) and data.get(_CORRELATION_KEY):
                _ctx_remember(
                    data[_CORRELATION_KEY],
                    {
                        "requested_model": data.get("model"),
                        "identity": _identity(user_api_key_dict),
                        "session": _session(
                            _request_headers(data), data.get("messages")
                        ),
                    },
                )
        except Exception:  # pragma: no cover - defensive
            pass
        return data

    # --- per-attempt trail (success + failure) -----------------------------
    # A STREAMED success event additionally yields the request's `delivered`
    # record (goal 29) — the post-stream success event is the only complete
    # per-request carrier for streams on this pin (async_post_call_success_hook
    # never runs for them; see _delivered_stream_record). Non-streamed events
    # return None there and emit nothing extra.
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "success"))
        delivered = _delivered_stream_record(kwargs)
        if delivered is not None:
            _emit(delivered)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "failure"))

    # Sync variants — the proxy path is async, but cover both so no attempt is
    # silently dropped if LiteLLM changes which it calls.
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "success"))
        delivered = _delivered_stream_record(kwargs)
        if delivered is not None:
            _emit(delivered)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "failure"))

    # --- per-request delivered summary (captures the fallback winner) -------
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        hp = getattr(response, "_hidden_params", {}) or {}
        usage = getattr(response, "usage", None)
        usage_d = usage.model_dump() if hasattr(usage, "model_dump") else {}
        requested = data.get("model")
        served = getattr(response, "model", None)
        record = {
            "event": "delivered",
            "requested_model": requested,
            "served_model": served,
            "served_model_id": (hp.get("model_id") or "")[:12] or None,
            "api_base": hp.get("api_base"),
            "provider": hp.get("custom_llm_provider"),
            "response_cost": hp.get("response_cost"),
            "tokens": {
                "prompt": usage_d.get("prompt_tokens"),
                "completion": usage_d.get("completion_tokens"),
                "total": usage_d.get("total_tokens"),
            },
            # A fallback served the request iff the backend that answered is
            # not the alias the client requested.
            "fallback": bool(requested and served and requested != served),
            # The JOIN KEY (goal 16): the request-scoped trace_id we stamped on
            # `data` in async_pre_call_hook, shared by every attempt (incl. a
            # fallback's failed primary). The dashboard nests this request's
            # `llm_call` attempts under it by this id.
            "correlation_id": data.get(_CORRELATION_KEY),
            # The WINNER's own call id (differs per attempt). When the winner's
            # success event DOES fire, it links this request to that exact
            # success attempt; on the fallback-winner path it may fire for no
            # attempt, which is why correlation_id (above) is the reliable join.
            "litellm_call_id": hp.get("litellm_call_id"),
        }
        # WHO asked (goal 15): stamp the caller's synthetic identity onto the
        # delivered record. Null under the master key / no key store, so the
        # bare-pytest + cli-auth profiles are unaffected.
        record.update(_identity(user_api_key_dict))
        # SHADOW complexity (goal 21): classified from the ORIGINAL request
        # (`data` is the proxy's request dict — messages + tools), inside a
        # post-response hook, so it can influence nothing. See _complexity.
        cx = _complexity(data.get("messages"), data.get("tools"))
        if cx is not None:
            record["complexity"] = cx
        # SHADOW session classification (goal 22): the inbound header map is
        # verified to reach data["metadata"]["headers"] on the pinned v1.83.14
        # (auth headers already stripped by LiteLLM; we read only the tags key).
        sess = _session(
            (data.get("metadata") or {}).get("headers"), data.get("messages")
        )
        if sess is not None:
            record["session"] = sess
        # SHADOW policy (goal 24): the authoritative chosen-vs-actual verdict.
        # actual = the backend that really served (served_model — reliable even
        # on the fallback path, which is why `delivered` exists at all);
        # agree = the policy's chosen == reality. Still pure telemetry.
        block = _policy_recall(data.get(_CORRELATION_KEY))
        if block is not None:
            record["shadow_policy"] = _policy_with_outcome(block, served)
        # Double-emission guard (goal 29): claim this request's correlation id
        # so the streamed carrier cannot also deliver it — and skip if that
        # carrier claimed it first. On the pinned v1.83.14 the carriers are
        # mutually exclusive (this hook never runs for streams), so on this
        # pin the claim always succeeds and behavior is unchanged; the guard
        # is insurance so a litellm upgrade that starts firing both can only
        # ever yield ONE delivered record per request, whichever lands first.
        if not _delivered_mark_once(data.get(_CORRELATION_KEY)):
            return
        _emit(record)


routing_recorder = RoutingRecorder()
