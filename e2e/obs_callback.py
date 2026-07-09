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

                ⚠️ LiteLLM quirk (verified against v1.83.14): on a proxy
                fallback the WINNING deployment does NOT fire a success event —
                only the failed primary attempt logs here. The winner is
                captured by the `delivered` record below instead. See
                docs/09-observability.md.

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
import uuid

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
    raw = headers.get(_SESSION_HEADER) if isinstance(headers, dict) else None
    if isinstance(raw, str):
        for tag in raw.split(","):
            tag = tag.strip()
            if tag.startswith(_SESSION_TAG_PREFIX) and len(tag) > len(
                _SESSION_TAG_PREFIX
            ):
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
    # the tag is simply omitted. Attempt-level stamping matters because STREAMED
    # requests fire no `delivered` record on this pin (docs/09) — the attempt
    # trail is their only carrier.
    cx = _complexity(
        kwargs.get("messages") or slo.get("messages"),
        (kwargs.get("optional_params") or {}).get("tools"),
    )
    if cx is not None:
        record["complexity"] = cx
    # SHADOW session classification (goal 22) — attempt-side stamping for the
    # same reason as complexity: streamed requests fire no `delivered` record
    # on this pin, so the attempt trail is their only carrier. Headers verified
    # to reach litellm_params.metadata.headers on v1.83.14 (see _session).
    sess = _session(
        ((kwargs.get("litellm_params") or {}).get("metadata") or {}).get("headers"),
        kwargs.get("messages") or slo.get("messages"),
    )
    if sess is not None:
        record["session"] = sess
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
        return data

    # --- per-attempt trail (success + failure) -----------------------------
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "success"))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "failure"))

    # Sync variants — the proxy path is async, but cover both so no attempt is
    # silently dropped if LiteLLM changes which it calls.
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _emit(_llm_call_record(kwargs, "success"))

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
        _emit(record)


routing_recorder = RoutingRecorder()
