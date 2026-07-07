"""Routing-record observability callback (goal 3).

Answers "where did my prompt go, why, how long, how many tokens, and did it
fall back?" for every request through the gateway — with NO external
observability stack (no Langfuse, no OTEL collector, no Postgres read).

It emits two record shapes, keyed by `event`:

  * llm_call  — one per BACKEND ATTEMPT (success OR failure). Carries the
                backend that was tried, its tier, per-attempt latency, tokens,
                and — on failure — the error that TRIGGERED a fallback (the
                "why", e.g. a 503/429). This is the attempt trail.

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


def _llm_call_record(kwargs, fallback_status: str) -> dict:
    """Build an `llm_call` record from a success/failure event's kwargs."""
    slo = kwargs.get("standard_logging_object") or {}
    err = slo.get("error_information") or {}
    rt = slo.get("response_time")
    return {
        "event": "llm_call",
        "status": slo.get("status") or fallback_status,
        # The alias the client asked for (the router "model group").
        "requested_group": slo.get("model_group"),
        # The concrete backend/deployment that this attempt hit.
        "backend": slo.get("model"),
        "backend_model_id": (slo.get("model_id") or "")[:12] or None,
        "api_base": slo.get("api_base"),
        "tier": _tier(kwargs),
        "latency_ms": round(rt * 1000, 1) if isinstance(rt, (int, float)) else None,
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
        _emit(record)


routing_recorder = RoutingRecorder()
