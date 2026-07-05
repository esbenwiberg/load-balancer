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

Sinks (independent, both optional):

  * stdout  — ALWAYS. One JSON object per line, prefixed `ROUTING_RECORD `.
              This is the production-friendly, dependency-free path: scrape it
              with `docker logs ... | grep ROUTING_RECORD` or ship it to any log
              collector. See docs/09-observability.md.
  * webhook — only if OBS_WEBHOOK_URL is set: POST each record there. The e2e
              stack points this at mockd's /__observe so the test suite can read
              records back over HTTP and assert on them. Fire-and-forget with a
              short timeout; ANY failure is swallowed so observability can never
              break the request path (logging runs post-response anyway).

Wire-up (litellm-config.*.yaml):  litellm_settings: { callbacks: obs_callback.routing_recorder }
The file must sit next to the config so LiteLLM can import it (it adds the
config dir to sys.path).
"""

from __future__ import annotations

import json
import os

from litellm.integrations.custom_logger import CustomLogger

try:  # httpx ships with litellm; guard anyway so an import hiccup can't wedge boot.
    import httpx
except Exception:  # pragma: no cover - defensive
    httpx = None

_WEBHOOK_URL = os.environ.get("OBS_WEBHOOK_URL", "").strip()
_WEBHOOK_TIMEOUT = float(os.environ.get("OBS_WEBHOOK_TIMEOUT", "2.0"))
_STDOUT_PREFIX = "ROUTING_RECORD "


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
    if not _WEBHOOK_URL or httpx is None:
        return
    # Blocking POST to a local sink (mockd is instant); runs post-response so it
    # adds no client latency. Any error is swallowed — observability is
    # best-effort and must never surface to the caller.
    try:
        httpx.post(_WEBHOOK_URL, json=record, timeout=_WEBHOOK_TIMEOUT)
    except Exception:  # pragma: no cover - defensive
        pass


def _tier(kwargs) -> str | None:
    md = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    return (md.get("model_info") or {}).get("backend_tier")


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
    }


class RoutingRecorder(CustomLogger):
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
        _emit(
            {
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
            }
        )


routing_recorder = RoutingRecorder()
