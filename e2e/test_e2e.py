"""
End-to-end tests for the balancer — raw HTTP, no real backends.

Drives the LiteLLM gateway (SUT) the way the real clients do:
  * Claude Code  -> Anthropic  POST /v1/messages
  * Codex        -> OpenAI     POST /v1/responses
  * generic      -> OpenAI     POST /v1/chat/completions

and asserts the balancer's own behaviour: protocol translation, the
Responses->Chat bridge (Blocker A), fallback on backend fault, virtual-key
model scoping, and streaming integrity. Backends are the mockd daemon, which we
drive out-of-band via /__control to inject faults deterministically.

Run via ./run.sh (brings the stack up first), or against an already-running
stack:  pytest test_e2e.py -v
Requires: httpx (see requirements.txt). The stack must be reachable at
GATEWAY_URL / MOCKD_URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:4000")
MOCKD = os.environ.get("MOCKD_URL", "http://localhost:9100")
DASH = os.environ.get("DASH_URL", "http://localhost:9300")  # goal-12 dashboard
CTRL = os.environ.get(
    "CONTROL_PLANE_URL", "http://localhost:9400"
)  # goal-13 fleet registry
KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-e2e-master-test-key")

AUTH = {"Authorization": "Bearer " + KEY}
TIMEOUT = 30.0


@pytest.fixture(autouse=True)
def _reset_mockd():
    """Clear all injected faults before each test so tests can't leak state.

    NOTE: this clears mockd's BACKEND fault state, but not any gateway-side
    router state. LiteLLM's per-deployment COOLDOWN is exactly such state — it is
    in-memory, time-based, and survives a mockd reset, so a fault test that
    flapped qwen3-coder would silently cool it down and reroute the *next*
    serial test's request to the fallback. We defuse that at the source by
    setting `disable_cooldowns: true` in litellm-config.e2e.yaml (see the comment
    there) rather than papering over it with waits here — cooldown is a latency
    optimization, and the client-visible contract the suite asserts is fallback,
    which is cooldown-independent.
    """
    httpx.post(MOCKD + "/__reset", timeout=TIMEOUT)
    _reset_dashboard()
    _reset_control_plane()
    yield
    httpx.post(MOCKD + "/__reset", timeout=TIMEOUT)
    _reset_dashboard()
    _reset_control_plane()


def _reset_control_plane():
    """Clear the goal-5 control-plane registry so fleet heartbeats never leak
    across serially-run tests (same contract as mockd/dashboard resets).
    Best-effort: a stack without the control-plane must not hard-fail here — the
    goal-13 fleet tests surface a real misconfig on their own."""
    try:
        httpx.post(CTRL + "/__reset", timeout=TIMEOUT)
    except httpx.HTTPError:
        pass


def _reset_dashboard():
    """Clear the goal-12 dashboard's record sink so routing records never leak
    across serially-run tests, exactly like mockd's /__reset. Best-effort: a
    bare `pytest` against a stack without the dashboard shouldn't hard-fail here,
    so a connection error is tolerated (the dashboard tests will surface a real
    misconfig loudly on their own)."""
    try:
        httpx.post(DASH + "/__reset", timeout=TIMEOUT)
    except httpx.HTTPError:
        pass


def _inject(directive):
    r = httpx.post(MOCKD + "/__control", json=directive, timeout=TIMEOUT)
    assert r.status_code == 200, r.text


# --- protocol translation ---------------------------------------------------


def test_anthropic_messages_streaming_translation():
    """Claude Code's native surface: Anthropic /v1/messages -> mockd (OpenAI)
    and back. Proves client-side Anthropic<->OpenAI translation.

    STREAMING on purpose — it's what Claude Code actually does, and (finding,
    docs/03) LiteLLM 1.83.14's NON-streaming /v1/messages -> openai chat backend
    drops the text content, while the streaming path is correct. See
    test_anthropic_messages_nonstream_content_quirk for the guard on that.
    """
    text = ""
    with httpx.stream(
        "POST",
        GATEWAY + "/v1/messages",
        headers={**AUTH, "anthropic-version": "2023-06-01"},
        json={
            "model": "qwen3-coder",
            "max_tokens": 128,
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :].strip()
            if payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "content_block_delta":
                text += ev.get("delta", {}).get("text", "")
    # mockd stamps the backend it served -> proves it reached the workbench.
    assert "served_model=qwen3-coder" in text, text


def test_anthropic_messages_nonstream_content_quirk():
    """GUARD on a real LiteLLM 1.83.14 finding: NON-streaming /v1/messages over
    an openai chat backend returns an empty content block (text dropped in the
    anthropic<-responses conversion). Usage still maps. Coding agents stream, so
    impact is low — but this test will start FAILING (xpass) if a LiteLLM bump
    fixes it, which is our signal to drop the streaming-only caveat in the docs.
    """
    r = httpx.post(
        GATEWAY + "/v1/messages",
        headers={**AUTH, "anthropic-version": "2023-06-01"},
        json={
            "model": "qwen3-coder",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    text = "".join(
        b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"
    )
    assert text == "", (
        "non-stream /v1/messages now returns content — LiteLLM may have fixed the "
        "bug; update docs/03 and make this the primary assertion. Got: " + repr(text)
    )


def test_openai_chat_translation():
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"] or ""
    assert "served_model=qwen3-coder" in content, r.text


def test_responses_bridge():
    """Codex's surface: OpenAI /v1/responses, bridged down to mockd's chat
    backend via use_chat_completions_api (Blocker A). Just the plumbing here;
    the full tool-calling gate is test_conformance_through_gateway below."""
    r = httpx.post(
        GATEWAY + "/v1/responses",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "input": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Responses output -> a message item with our stamped text.
    text = ""
    for item in body.get("output", []):
        for c in item.get("content", []) or []:
            text += c.get("text", "")
    assert "served_model=qwen3-coder" in text, body


def test_malformed_tool_call_through_responses_bridge():
    """A malformed tool call surfaced through the Responses bridge (goal 6c).

    mockd's `malformed` mode emits a real tool call whose JSON arguments are
    truncated (closing brace dropped) — the classic "model produced invalid
    tool-call JSON" failure. Codex's path is /v1/responses bridged down to the
    chat backend, so this exercises the bridge's tool-call translation on a
    corrupt payload.

    Observed contract: the bridge is a TRANSPORT, not a validator. It surfaces
    the malformed arguments to the client VERBATIM as a function_call item — it
    neither repairs the JSON nor rejects the turn with a 5xx. Parsing/validation
    is the client's job (the SDK's json.loads on `arguments` is where it fails),
    which is the correct layering: the gateway must not silently mutate a tool
    call. If a LiteLLM bump starts sanitising or erroring here, this flips.
    """
    _inject({"model": "qwen3-coder", "mode": "malformed"})
    r = httpx.post(
        GATEWAY + "/v1/responses",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "tool_choice": "required",  # force the scripted read_file call
            "input": [{"role": "user", "content": "read the config"}],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, (
        "bridge must not 5xx on a malformed tool call: " + r.text
    )
    body = r.json()
    calls = [it for it in body.get("output", []) if it.get("type") == "function_call"]
    assert calls, (
        "expected a function_call to surface through the bridge, got: "
        + repr(body.get("output"))
    )
    raw_args = calls[0].get("arguments", "")
    assert raw_args, "function_call surfaced with no arguments string: " + repr(
        calls[0]
    )
    # The whole point: the args are passed through corrupt, not repaired.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw_args)


# --- negative paths: bad input -> clean 4xx, never a hang or 5xx (goal 8) ----
#
# The bound on "no hang" is the request completing inside TIMEOUT at all: httpx
# raises ReadTimeout if the gateway wedges, which fails the test. So every
# assertion here doubles as a liveness check — a 4xx that arrives is proof the
# gateway rejected cleanly instead of hanging or melting into a 5xx.


@pytest.mark.parametrize(
    "path",
    ["/v1/chat/completions", "/v1/responses", "/v1/messages"],
)
def test_malformed_json_body_clean_4xx(path):
    """A body that isn't valid JSON must be rejected with a clean 4xx on EVERY
    client surface — not a 5xx, not a hang. This is the classic garbage-in
    probe: a truncated/corrupt request must never wedge a worker."""
    headers = {**AUTH, "Content-Type": "application/json"}
    if path == "/v1/messages":
        headers["anthropic-version"] = "2023-06-01"
    r = httpx.post(
        GATEWAY + path,
        headers=headers,
        content=b'{"model": "qwen3-coder", "messages": [',  # truncated JSON
        timeout=TIMEOUT,
    )
    assert 400 <= r.status_code < 500, (
        "malformed JSON on %s must be a clean 4xx, got %s: %s"
        % (path, r.status_code, r.text)
    )


@pytest.mark.parametrize(
    "path,payload",
    [
        (
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "ping"}]},
        ),
        ("/v1/responses", {"input": [{"role": "user", "content": "ping"}]}),
        (
            "/v1/messages",
            {"max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
        ),
    ],
)
def test_unknown_model_alias_clean_4xx(path, payload):
    """A model alias the gateway doesn't know must be refused with a clean 4xx
    (LiteLLM's 'Invalid model name' family), never a 5xx or a hang. Proves the
    router rejects unroutable aliases at the door instead of failing downstream."""
    headers = {**AUTH}
    if path == "/v1/messages":
        headers["anthropic-version"] = "2023-06-01"
    body = {"model": "no-such-model-alias-xyz", **payload}
    r = httpx.post(GATEWAY + path, headers=headers, json=body, timeout=TIMEOUT)
    assert 400 <= r.status_code < 500, (
        "unknown model alias on %s must be a clean 4xx, got %s: %s"
        % (path, r.status_code, r.text)
    )


# --- fallback ----------------------------------------------------------------


def test_fallback_to_foundry_on_5xx():
    """Workbench 503s -> LiteLLM fallback chain advances to the Foundry tier.
    mockd's served_model stamp survives translation and proves WHICH backend
    actually answered."""
    _inject({"model": "qwen3-coder", "status": 503})  # persistent until reset
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"] or ""
    # First entry in the fallback chain is claude-sonnet.
    assert "served_model=claude-sonnet" in content, (
        "expected fallback to claude-sonnet, got: " + content
    )


def test_fallback_cascades_when_first_fallback_also_down():
    """Workbench AND claude-sonnet down -> should land on claude-opus."""
    _inject({"model": "qwen3-coder", "status": 503})
    _inject({"model": "claude-sonnet", "status": 503})
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"] or ""
    assert "served_model=claude-opus" in content, content


def test_fallback_on_429():
    """429 (rate-limit) is a fallback-triggering fault just like 5xx (goal 6a).

    A persistent 429 on the workbench must advance the fallback chain to the
    Foundry tier, not surface the 429 to the client. In production a repeated
    429 would ALSO trip the router's cooldown (`allowed_fails`/`cooldown_time`),
    pre-emptively skipping qwen3-coder on later requests — but cooldown is a
    latency optimization layered on TOP of fallback, and it is deliberately
    disabled in the e2e config (`disable_cooldowns: true`) because its in-memory,
    time-based state bleeds across serially-run tests. The client-visible
    contract this test pins is cooldown-independent: a clean 200 served by the
    fallback, never a leaked 429.
    """
    _inject({"model": "qwen3-coder", "status": 429})  # persistent until reset
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, "429 must fall back cleanly, not surface: " + r.text
    content = r.json()["choices"][0]["message"]["content"] or ""
    assert "served_model=claude-sonnet" in content, (
        "expected 429 to fall back to claude-sonnet, got: " + content
    )


def _served_model(resp_json) -> str:
    """Pull mockd's served_model stamp out of a chat completion."""
    return resp_json["choices"][0]["message"]["content"] or ""


def test_transient_5xx_retries_same_backend_before_fallback():
    """PIN the retry-vs-fallback ORDER (goal 6b). This is the config-change
    tripwire: LiteLLM's `num_retries` (=1 in the e2e config) is spent RETRYING
    THE SAME BACKEND before the fallback chain is ever consulted.

    mockd's count-limited fault lets us prove the order by observation, because
    which backend answers depends on whether the retry lands on the original:

      * count=1  -> the FIRST attempt 503s and the fault auto-clears, so the one
                    retry hits qwen3-coder again and SUCCEEDS. served_model is
                    still qwen3-coder => LiteLLM retried the same backend and
                    never touched the fallback chain.
      * count=2  -> BOTH the first attempt and its retry 503 (fault outlasts the
                    retry budget); only THEN does the chain advance, so
                    claude-sonnet answers.

    The contrast nails it: a transient fault shorter than the retry budget is
    absorbed on the same backend; one that outlasts it advances the chain. If a
    future config bump reorders this (e.g. fallback-before-retry, or a retry that
    re-sends to a *different* deployment), count=1 would start answering from the
    Foundry tier and this test flips red. The observed order is documented in
    docs/03-open-questions-and-risks.md (risk 7).
    """
    # One transient 503, shorter than the retry budget -> absorbed on retry.
    _inject({"model": "qwen3-coder", "status": 503, "count": 1})
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    assert "served_model=qwen3-coder" in _served_model(r.json()), (
        "a transient fault within the retry budget must be absorbed on the SAME "
        "backend (num_retries retried qwen3-coder before any fallback), got: "
        + _served_model(r.json())
    )

    # A fault that outlasts the retry budget (first attempt + its one retry both
    # 503) -> retries exhausted, chain advances to the Foundry tier.
    httpx.post(MOCKD + "/__reset", timeout=TIMEOUT)
    _inject({"model": "qwen3-coder", "status": 503, "count": 2})
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    assert "served_model=claude-sonnet" in _served_model(r.json()), (
        "a fault outlasting the retry budget must advance the fallback chain, "
        "got: " + _served_model(r.json())
    )


# --- mid-stream backend death (docs/03 risk 7) ------------------------------


def _drain_stream(url, payload, read_timeout=5.0):
    """Open an SSE stream and drain it, classifying HOW it terminated.

    Returns (status, chunks, done_seen, truncated):
      * done_seen  -> the stream ended with a clean terminator ([DONE])
      * truncated  -> the stream stopped WITHOUT one (connection died or went
                      silent past `read_timeout`) — the mid-stream-death
                      signature we're probing for.
    A tight read timeout bounds the post-hangup hang: mockd answers instantly,
    so the only long read is the one that never completes after the backend dies.
    """
    chunks, done_seen, truncated, status = [], False, False, None
    timeout = httpx.Timeout(TIMEOUT, read=read_timeout)
    try:
        with httpx.stream(
            "POST",
            url,
            headers={**AUTH, "anthropic-version": "2023-06-01"},
            json=payload,
            timeout=timeout,
        ) as resp:
            status = resp.status_code
            try:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    data = line[len("data: ") :] if line.startswith("data: ") else line
                    if data.strip() == "[DONE]":
                        done_seen = True
                        continue
                    chunks.append(data)
            except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
                truncated = True
    except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
        truncated = True
    return status, chunks, done_seen, truncated


def test_chat_stream_backend_hangup_midstream():
    """docs/03 risk 7: the backend dies mid-stream AFTER the gateway has already
    committed HTTP 200 + forwarded bytes to the client.

    Observed behaviour (documented reason it CANNOT cleanly fall back): the
    response line is already on the wire, so LiteLLM cannot re-route — the client
    receives the partial pre-hangup content and then a truncated stream that
    never emits [DONE]. Crucially the gateway does NOT silently re-send to
    another backend (no Foundry-tier stamp leaks in), so there's no
    duplicate-request / spliced-reply corruption — the failure is a clean
    truncation the client must detect via a missing terminator, not a bad merge.
    """
    _inject({"model": "qwen3-coder", "mode": "hangup"})
    status, chunks, done_seen, truncated = _drain_stream(
        GATEWAY + "/v1/chat/completions",
        {
            "model": "qwen3-coder",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    blob = " ".join(chunks)
    assert status == 200, "200 is committed before the mid-stream death"
    assert "partial ..." in blob, "expected the partial pre-hangup chunk, got: " + blob
    assert not done_seen, "stream unexpectedly terminated cleanly with [DONE]"
    assert truncated, "expected a truncated, non-terminating stream on mid-stream death"
    # No mid-stream fallback: a Foundry-tier backend must NOT have answered into
    # the same already-open response (that would splice two replies together).
    assert "served_model=claude" not in blob, (
        "gateway fell back mid-stream — would corrupt an already-streamed reply: "
        + blob
    )


def test_responses_stream_backend_hangup_midstream():
    """Same mid-stream death over Codex's /v1/responses bridge. Same verdict:
    the partial output_text.delta reaches the client, then the stream truncates
    with no response.completed / [DONE] and no fallback stamp."""
    _inject({"model": "qwen3-coder", "mode": "hangup"})
    status, chunks, done_seen, truncated = _drain_stream(
        GATEWAY + "/v1/responses",
        {
            "model": "qwen3-coder",
            "stream": True,
            "input": [{"role": "user", "content": "ping"}],
        },
    )
    blob = " ".join(chunks)
    assert status == 200, "200 is committed before the mid-stream death"
    assert "partial ..." in blob, "expected the partial pre-hangup delta, got: " + blob
    assert not done_seen, "stream unexpectedly terminated cleanly with [DONE]"
    assert truncated, "expected a truncated, non-terminating stream on mid-stream death"
    assert "response.completed" not in blob, (
        "stream emitted response.completed despite mid-stream death: " + blob
    )
    assert "served_model=claude" not in blob, (
        "gateway fell back mid-stream on the Responses bridge: " + blob
    )


# --- auth / virtual keys -----------------------------------------------------


def test_virtual_key_model_scoping():
    """A virtual key scoped to ['gpt'] must be refused for qwen3-coder."""
    gen = httpx.post(
        GATEWAY + "/key/generate",
        headers=AUTH,
        json={"models": ["gpt"], "user_id": "e2e-scope-test"},
        timeout=TIMEOUT,
    )
    assert gen.status_code == 200, gen.text
    scoped = gen.json()["key"]

    # Allowed model works.
    ok = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers={"Authorization": "Bearer " + scoped},
        json={"model": "gpt", "messages": [{"role": "user", "content": "ping"}]},
        timeout=TIMEOUT,
    )
    assert ok.status_code == 200, ok.text

    # Disallowed model is refused.
    denied = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers={"Authorization": "Bearer " + scoped},
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert denied.status_code in (400, 401, 403), (
        "scoped key should be refused for qwen3-coder, got %s: %s"
        % (denied.status_code, denied.text)
    )


def test_missing_auth_rejected():
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code in (401, 403), r.text


# --- budgets + rate limits (goal 11) -----------------------------------------
# Config defaults live in litellm-config.e2e.yaml -> litellm_settings.
# default_key_generate_params. Keep these mirrored with that file.
DEFAULT_MAX_BUDGET = 100.0
DEFAULT_RPM_LIMIT = 60
DEFAULT_TPM_LIMIT = 200000

_CHAT = {"model": "qwen3-coder", "messages": [{"role": "user", "content": "ping"}]}


def _generate_key(**params):
    """Mint a virtual key via the master key; return (key_string, full_response)."""
    r = httpx.post(
        GATEWAY + "/key/generate", headers=AUTH, json=params, timeout=TIMEOUT
    )
    assert r.status_code == 200, "key/generate failed: " + r.text
    return r.json()["key"], r.json()


def test_issued_key_inherits_default_budget_and_limits():
    """The wallet guardrail: a key minted with NO budget/limit fields must still
    come back carrying the config defaults (default_key_generate_params). This is
    the 'every key the gateway issues gets a default' half of goal 11 — without
    it, a bare /key/generate would mint an unlimited key and the backstop leaks.
    """
    _, resp = _generate_key(models=["qwen3-coder"], user_id="e2e-default-budget")
    assert resp.get("max_budget") == DEFAULT_MAX_BUDGET, (
        "issued key missing default max_budget: %r" % resp.get("max_budget")
    )
    assert resp.get("rpm_limit") == DEFAULT_RPM_LIMIT, (
        "issued key missing default rpm_limit: %r" % resp.get("rpm_limit")
    )
    assert resp.get("tpm_limit") == DEFAULT_TPM_LIMIT, (
        "issued key missing default tpm_limit: %r" % resp.get("tpm_limit")
    )


def test_over_budget_key_refused_clean_4xx():
    """An over-budget key is refused with a clean 4xx — no hang, no 5xx.

    Uses an explicit max_budget:0 key so the `spend >= max_budget` gate trips on
    the FIRST request (0 >= 0), which is deterministic: it needs neither async
    spend-flush nor a costed model, both of which would make a CI gate flaky.
    An explicit value overrides the config default, so the guardrail's plumbing
    (config default) and its teeth (this refusal) are proven independently.
    """
    key, _ = _generate_key(
        models=["qwen3-coder"], max_budget=0, user_id="e2e-over-budget"
    )
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers={"Authorization": "Bearer " + key},
        json=_CHAT,
        timeout=TIMEOUT,  # a hang would raise ReadTimeout -> test fails, not stalls
    )
    assert 400 <= r.status_code < 500, (
        "over-budget key must get a clean 4xx, got %s: %s" % (r.status_code, r.text)
    )
    assert "budget" in r.text.lower(), "expected a budget-exceeded reason: " + r.text


def test_over_rate_limit_key_refused_clean_4xx():
    """An over-rate-limit key is refused with a clean 4xx (429) — no hang, no 5xx.

    Mints an rpm_limit:1 key and fires a rapid burst. LiteLLM's limiter resets on
    the UTC-minute boundary, so we do NOT assert 'every request after the first is
    429' (a burst straddling the boundary would see a fresh 200) — that would be
    flaky. The robust contract: the first request succeeds, at least one request
    in the burst is refused with 429, and NOTHING returns a 5xx or hangs.
    """
    key, _ = _generate_key(models=["qwen3-coder"], rpm_limit=1, user_id="e2e-over-rpm")
    codes = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for _ in range(6):
            resp = client.post(
                GATEWAY + "/v1/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=_CHAT,
            )
            codes.append(resp.status_code)

    assert codes[0] == 200, "first request under the limit should pass: %r" % codes
    assert 429 in codes, "an over-limit burst must yield at least one 429: %r" % codes
    assert all(c in (200, 429) for c in codes), (
        "rate limiting must be a clean 200/429 split, never a 5xx: %r" % codes
    )


# --- streaming ---------------------------------------------------------------


def test_streaming_chat_integrity():
    """Stream a chat completion; assert we get multiple SSE chunks and a clean
    [DONE], i.e. streaming survives the gateway hop."""
    chunks = []
    saw_done = False
    with httpx.stream(
        "POST",
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line:
                continue
            data = line[len("data: ") :] if line.startswith("data: ") else line
            if data.strip() == "[DONE]":
                saw_done = True
                continue
            chunks.append(data)
    assert len(chunks) >= 1, "expected at least one streamed chunk"
    assert saw_done, "stream did not terminate with [DONE]"


# --- concurrency: parallel streams must not cross-talk (goal 9) --------------
#
# Every other test runs serially, but the gateway's whole job is serving
# concurrent agents. Two catastrophic-but-invisible bugs live here:
#   1. a WRONG served_model stamp — request A's backend identity bleeding into
#      request B's response (per-request router/translation state shared across
#      workers or coroutines);
#   2. INTERLEAVED SSE — chunks from two open streams spliced into one wire.
# The gateway runs with --num_workers 2 (see docker-compose.e2e.yaml), so these
# requests genuinely fan out across worker processes, which is exactly where
# such state bleed would hide. A fault is injected on ONE alias so a fallback
# hop happens IN THE MIDDLE of the concurrent fleet: if fallback leaked state,
# a sibling stream would pick up the Foundry-tier stamp.

# The full set of backend stamps mockd can emit. Used to prove NO foreign stamp
# bleeds into a response that didn't ask for it.
ALL_SERVED = ("qwen3-coder", "claude-sonnet", "claude-opus", "gpt")


def _stream_chat(alias):
    """Drive one streaming chat completion to completion. Returns
    (status, served_text, done_seen). served_text is the concatenated content,
    which carries mockd's `served_model=<backend>` stamp."""
    text, done_seen, status = "", False, None
    with httpx.stream(
        "POST",
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": alias,
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    ) as resp:
        status = resp.status_code
        for line in resp.iter_lines():
            if not line:
                continue
            data = line[len("data: ") :] if line.startswith("data: ") else line
            if data.strip() == "[DONE]":
                done_seen = True
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            for ch in ev.get("choices", []):
                text += (ch.get("delta", {}) or {}).get("content") or ""
    return status, text, done_seen


def _assert_clean_stamped(alias, expected, status, text, done_seen):
    """Every concurrent response must: be a 200, terminate cleanly with [DONE],
    carry EXACTLY its own expected backend stamp, and carry NO foreign stamp."""
    assert status == 200, "%s: expected 200, got %s" % (alias, status)
    assert done_seen, "%s: stream did not terminate cleanly with [DONE]" % alias
    assert ("served_model=%s" % expected) in text, (
        "%s: wrong/absent served_model stamp — expected %s, got: %r"
        % (alias, expected, text)
    )
    for other in ALL_SERVED:
        if other == expected:
            continue
        assert ("served_model=%s" % other) not in text, (
            "%s: FOREIGN stamp %s bled into this response — concurrent "
            "cross-talk. Got: %r" % (alias, other, text)
        )


def test_concurrent_chat_streams_no_crosstalk():
    """Fire a fleet of concurrent streaming requests across FOUR distinct model
    aliases, with a persistent 503 on qwen3-coder so its requests fall back to
    claude-sonnet mid-fleet. Assert every response carries the correct
    served_model stamp (the backend that actually answered — the fallback target
    for the faulted alias, itself for the rest), no foreign stamp bled in, and
    every stream terminated cleanly with [DONE].

    This is the goal-9 smoke: the discriminator is that four different backends
    answer at once and their identity stamps must not swap. If per-request state
    were shared across the two gateway workers, a claude-opus request could come
    back stamped qwen3-coder (or vice-versa) and this goes red.
    """
    _inject({"model": "qwen3-coder", "status": 503})  # persistent -> fallback

    # (requested alias, backend expected to actually serve it). qwen3-coder is
    # faulted so it falls back to the first Foundry-tier entry, claude-sonnet;
    # the others serve themselves. Repeated to raise contention across workers.
    fleet = [
        ("qwen3-coder", "claude-sonnet"),
        ("claude-sonnet", "claude-sonnet"),
        ("claude-opus", "claude-opus"),
        ("gpt", "gpt"),
    ] * 3  # 12 concurrent streams

    with ThreadPoolExecutor(max_workers=len(fleet)) as ex:
        results = list(ex.map(lambda pair: _stream_chat(pair[0]), fleet))

    for (alias, expected), (status, text, done_seen) in zip(fleet, results):
        _assert_clean_stamped(alias, expected, status, text, done_seen)


# --- cross-surface concurrency: interleaved SSE across protocols -------------


def _stream_responses(alias):
    """Drive one streaming /v1/responses (Codex bridge). Clean termination is
    response.completed (LiteLLM's Responses terminator)."""
    text, done_seen, status = "", False, None
    with httpx.stream(
        "POST",
        GATEWAY + "/v1/responses",
        headers=AUTH,
        json={
            "model": alias,
            "stream": True,
            "input": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    ) as resp:
        status = resp.status_code
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :].strip()
            if data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "response.output_text.delta":
                text += ev.get("delta", "") or ""
            elif ev.get("type") == "response.completed":
                done_seen = True
    return status, text, done_seen


def _stream_anthropic(alias):
    """Drive one streaming /v1/messages (Claude Code's native surface). Clean
    termination is the message_stop event."""
    text, done_seen, status = "", False, None
    with httpx.stream(
        "POST",
        GATEWAY + "/v1/messages",
        headers={**AUTH, "anthropic-version": "2023-06-01"},
        json={
            "model": alias,
            "max_tokens": 128,
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    ) as resp:
        status = resp.status_code
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :].strip()
            if data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "content_block_delta":
                text += ev.get("delta", {}).get("text", "") or ""
            elif t == "message_stop":
                done_seen = True
    return status, text, done_seen


def test_concurrent_mixed_surface_streams_no_crosstalk():
    """The nastier variant: all THREE client surfaces streaming at once, each
    asking for a different alias, with the fault still on qwen3-coder. This is
    the interleaved-SSE-across-protocols probe — an Anthropic content_block_delta
    must never end up spliced into a Responses or Chat stream, and every surface
    must still deliver its own backend's stamp and its own clean terminator.

    Each (surface, alias) pair is fired concurrently and repeated so multiple
    streams of each protocol are open simultaneously.
    """
    _inject({"model": "qwen3-coder", "status": 503})  # persistent -> fallback

    # (drain fn, requested alias, expected served backend)
    jobs = [
        (_stream_chat, "claude-opus", "claude-opus"),
        (_stream_responses, "gpt", "gpt"),
        (_stream_anthropic, "claude-sonnet", "claude-sonnet"),
        (_stream_chat, "qwen3-coder", "claude-sonnet"),  # faulted -> fallback
        (_stream_responses, "claude-opus", "claude-opus"),
        (_stream_anthropic, "gpt", "gpt"),
    ] * 2  # 12 concurrent streams, mixed across the three surfaces

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(lambda j: j[0](j[1]), jobs))

    for (fn, alias, expected), (status, text, done_seen) in zip(jobs, results):
        surface = fn.__name__.replace("_stream_", "")
        _assert_clean_stamped(
            "%s[%s]" % (surface, alias), expected, status, text, done_seen
        )


# --- observability: per-request routing records (goal 3) ---------------------
#
# The gateway's obs_callback (litellm-config.e2e.yaml -> litellm_settings.
# callbacks) publishes a routing record per backend ATTEMPT (event=llm_call) and
# per DELIVERED response (event=delivered) to mockd's /__observe sink. These
# prove we capture {chosen backend, why, latency, tokens, fallback-hit} for every
# request with NO external observability stack. mockd is one process, so it
# centralizes records across BOTH gateway workers; /__reset (autouse fixture)
# clears them, so each test sees only its own. See docs/09-observability.md.


def _observe():
    """All routing records mockd has received so far."""
    r = httpx.get(MOCKD + "/__observe", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json().get("records", [])


def _poll_observe(predicate, timeout=8.0):
    """The callback POSTs records AFTER the client response returns, so the store
    lags the HTTP 200. Poll until `predicate(records)` holds (or time out), then
    return the records — a final read either way so the assertion sees the best
    available snapshot for its error message."""
    deadline = time.time() + timeout
    recs = _observe()
    while time.time() < deadline:
        if predicate(recs):
            return recs
        time.sleep(0.25)
        recs = _observe()
    return recs


def test_fallback_is_observable_in_routing_record():
    """Goal 3: a fallback must be OBSERVABLE in the captured records, not only in
    the client-visible response. Force qwen3-coder -> claude-sonnet, then read the
    records the gateway's obs_callback published and assert both halves of the
    story are captured:

      * a `delivered` record shows requested_model=qwen3-coder but
        served_model=claude-sonnet with fallback=true, carrying token usage —
        the CHOSEN backend and the FALLBACK-HIT flag; and
      * an `llm_call` failure record for qwen3-coder with error_code 503, the
        backend tier, and a captured latency — the WHY behind the fallback.

    Together: the {chosen backend, why, latency, tokens, fallback-hit} the
    observability goal requires. We poll for BOTH before asserting so the async
    log flush can't race the read.
    """
    _inject({"model": "qwen3-coder", "status": 503})  # persistent -> fallback
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    assert "served_model=claude-sonnet" in _served_model(r.json()), r.text

    def _both_captured(recs):
        has_delivered = any(
            x.get("event") == "delivered"
            and x.get("requested_model") == "qwen3-coder"
            and x.get("fallback") is True
            for x in recs
        )
        has_failure = any(
            x.get("event") == "llm_call"
            and x.get("status") == "failure"
            and x.get("requested_group") == "qwen3-coder"
            for x in recs
        )
        return has_delivered and has_failure

    recs = _poll_observe(_both_captured)

    # (a) the delivered record: chosen backend + fallback flag + tokens
    delivered = [
        x
        for x in recs
        if x.get("event") == "delivered" and x.get("requested_model") == "qwen3-coder"
    ]
    assert delivered, "no delivered record for the qwen3-coder request: %r" % recs
    d = delivered[-1]
    assert d.get("fallback") is True, "delivered record must flag the fallback: %r" % d
    assert d.get("served_model") == "claude-sonnet", (
        "delivered record must name the backend that actually served: %r" % d
    )
    assert (d.get("tokens") or {}).get("total"), (
        "delivered record must capture token usage: %r" % d
    )

    # (b) the failed-attempt record: the WHY (503 on the workbench) + latency
    failed = [
        x
        for x in recs
        if x.get("event") == "llm_call"
        and x.get("status") == "failure"
        and x.get("requested_group") == "qwen3-coder"
    ]
    assert failed, "no failed-attempt record explaining the fallback: %r" % recs
    f = failed[-1]
    assert str(f.get("error_code")) == "503", (
        "failure record must capture the 503 that triggered the fallback: %r" % f
    )
    assert f.get("tier") == "local", (
        "failure record should carry the faulted backend's tier: %r" % f
    )
    assert isinstance(f.get("latency_ms"), (int, float)), (
        "failure record must capture per-attempt latency: %r" % f
    )


def test_direct_request_routing_record_no_fallback():
    """The baseline the fallback case is distinguished against: a request its own
    backend serves records fallback=false, names that backend, and the per-attempt
    `llm_call` carries latency + tokens. Guards against a fallback flag that is
    accidentally always-true (which would make the fallback assertion vacuous)."""
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text

    def _both_seen(recs):
        # The `delivered` POST and the `llm_call` success POST arrive in no
        # guaranteed order, so wait for BOTH — the test asserts on each.
        has_delivered = any(
            x.get("event") == "delivered"
            and x.get("requested_model") == "claude-sonnet"
            for x in recs
        )
        has_call = any(
            x.get("event") == "llm_call" and x.get("status") == "success" for x in recs
        )
        return has_delivered and has_call

    recs = _poll_observe(_both_seen)
    delivered = [
        x
        for x in recs
        if x.get("event") == "delivered" and x.get("requested_model") == "claude-sonnet"
    ]
    assert delivered, "no delivered record for the direct request: %r" % recs
    d = delivered[-1]
    assert d.get("fallback") is False, (
        "a directly-served request must NOT be flagged as a fallback: %r" % d
    )
    assert d.get("served_model") == "claude-sonnet", d

    calls = [
        x for x in recs if x.get("event") == "llm_call" and x.get("status") == "success"
    ]
    assert calls, "no successful llm_call attempt record: %r" % recs
    c = calls[-1]
    assert isinstance(c.get("latency_ms"), (int, float)), (
        "attempt record must capture latency: %r" % c
    )
    assert (c.get("tokens") or {}).get("total"), (
        "attempt record must capture token usage: %r" % c
    )


# --- routing dashboard v1: "where did my prompt go?" (goal 12) ---------------
#
# The dashboard (e2e/dashboard.py) is the read-only, visible face of goal-3's
# routing records. The gateway's obs_callback fans each record to BOTH sinks
# (mockd/__observe AND dashboard/records — see docker-compose.e2e.yaml
# OBS_WEBHOOK_URL). The dashboard folds them into a per-REQUEST view (requested
# alias -> served backend, fallback flag, tokens) + a per-ATTEMPT trail (the
# "why" behind a fallback). GET /api/records is the DATA ENDPOINT the UI fetches;
# these tests assert on it directly, so a green suite proves the data the page
# renders is really there. See docs/09-observability.md.
#
# BUILD-vs-REUSE (reversible call, documented in dashboard.py + docs/09): we BUILD
# a thin read-only page over goal-3 data rather than reuse LiteLLM's admin UI —
# the routing-record shape (fallback "why", backend tier) is ours, not LiteLLM's,
# and an owned JSON endpoint is deterministically assertable where a React SPA
# behind master-key auth is not.


def _dash_api():
    """The dashboard's data endpoint — what the read-only page fetches."""
    r = httpx.get(DASH + "/api/records", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _poll_dash(predicate, timeout=8.0):
    """Records reach the dashboard via a post-response webhook, so the sink lags
    the client's HTTP 200 (same race as mockd/__observe). Poll until the
    predicate holds, then return the latest snapshot either way."""
    deadline = time.time() + timeout
    data = _dash_api()
    while time.time() < deadline:
        if predicate(data):
            return data
        time.sleep(0.25)
        data = _dash_api()
    return data


def test_dashboard_data_endpoint_shows_direct_request():
    """Goal 12: a prompt just sent through the gateway shows up in the dashboard's
    data endpoint as a per-request routing record — requested alias == served
    backend (no fallback), with token usage. This is the endpoint the read-only
    page renders, asserted directly."""
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "ping dashboard direct"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text

    def _has_request(data):
        return any(
            rq.get("requested_model") == "claude-sonnet"
            for rq in data.get("requests", [])
        )

    data = _poll_dash(_has_request)
    reqs = [
        rq
        for rq in data.get("requests", [])
        if rq.get("requested_model") == "claude-sonnet"
    ]
    assert reqs, (
        "dashboard data endpoint has no request row for claude-sonnet: %r" % data
    )
    rq = reqs[0]  # newest first
    assert rq.get("served_model") == "claude-sonnet", rq
    assert rq.get("fallback") is False, (
        "a directly-served request must not be flagged a fallback on the dashboard: %r"
        % rq
    )
    assert rq.get("tokens_total"), (
        "dashboard request row must carry token usage: %r" % rq
    )


def test_dashboard_data_endpoint_shows_fallback_route():
    """Goal 12: the dashboard must make a FALLBACK legible — the whole point of
    "where did my prompt go?". Force qwen3-coder -> claude-sonnet, then assert the
    data endpoint carries BOTH halves:

      * a per-request row: requested qwen3-coder, served claude-sonnet, fallback
        flagged true; and
      * a per-attempt failure row for qwen3-coder naming the 503 that triggered
        the fallback (the "why") with its tier.
    """
    _inject({"model": "qwen3-coder", "status": 503})  # persistent -> fallback
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping dashboard fallback"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    assert "served_model=claude-sonnet" in _served_model(r.json()), r.text

    def _both(data):
        req = any(
            rq.get("requested_model") == "qwen3-coder" and rq.get("fallback") is True
            for rq in data.get("requests", [])
        )
        att = any(
            a.get("requested_group") == "qwen3-coder" and a.get("status") == "failure"
            for a in data.get("attempts", [])
        )
        return req and att

    data = _poll_dash(_both)

    # (a) the per-request row — the fallback made visible.
    reqs = [
        rq
        for rq in data.get("requests", [])
        if rq.get("requested_model") == "qwen3-coder"
    ]
    assert reqs, "dashboard has no request row for the qwen3-coder prompt: %r" % data
    rq = reqs[0]
    assert rq.get("fallback") is True, "dashboard must flag the fallback: %r" % rq
    assert rq.get("served_model") == "claude-sonnet", (
        "dashboard must name the backend that actually served: %r" % rq
    )

    # (b) the per-attempt failure row — the WHY behind the fallback.
    fails = [
        a
        for a in data.get("attempts", [])
        if a.get("requested_group") == "qwen3-coder" and a.get("status") == "failure"
    ]
    assert fails, (
        "dashboard attempt trail is missing the failed qwen3-coder attempt: %r" % data
    )
    f = fails[0]
    assert str(f.get("error_code")) == "503", (
        "dashboard attempt row must carry the 503 that triggered the fallback: %r" % f
    )
    assert f.get("tier") == "local", (
        "dashboard attempt row should carry the backend tier: %r" % f
    )


def test_dashboard_page_renders():
    """The read-only page itself serves (GET /) and is wired to its data endpoint.
    Not a headless-browser test — we assert the HTML is served and references the
    /api/records fetch, so the served page and the asserted data endpoint can't
    silently drift apart."""
    r = httpx.get(DASH + "/", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers.get("content-type", ""), r.headers
    body = r.text
    assert "Router dashboard" in body, "dashboard page title missing"
    assert "api/records" in body, "dashboard page must fetch its data endpoint"
    # Goal 13: the same page also renders + fetches the fleet view.
    assert "api/fleet" in body, "dashboard page must fetch the fleet data endpoint"
    assert "Fleet" in body, "dashboard page must render a Fleet section"
    # Goal 15: the page renders the identity ("who asked") — per-key rollup.
    assert "Per key" in body, "dashboard page must render a per-key rollup section"


# --- identity in routing records: WHO asked? (goal 15) -----------------------
#
# Goal 3 answered "where did my prompt go?"; goal 12 made it visible. Neither
# knew WHOSE prompt it was — obs_callback received `user_api_key_dict` and threw
# it away. Goal 15 stamps the caller's synthetic identity {key_alias, user_id,
# team_id} onto the `delivered` record (sourced from UserAPIKeyAuth, null under
# the master key / no key store) and the dashboard surfaces it: on each request
# row AND as a per-key rollup (requests, fallbacks, tokens, cost).
#
# This test mints a key bound to a SYNTHETIC alias+user+team (goal 11b's
# machinery), drives a request WITH THAT KEY (not the master key the other tests
# use), and asserts the identity round-trips all the way to the dashboard's owned
# /api/records — the same endpoint the read-only page renders.
#
# GUARDRAIL: identities are synthetic (repo-a-ish alias, e2e-user id), never a
# real name or email. No PII, per CLAUDE.md.


def test_dashboard_shows_minted_key_identity():
    """Goal 15: a request made with a MINTED key surfaces that key's
    alias+user+team in the dashboard's /api/records — both on the per-request row
    and in the per-key rollup. Proves the identity path end to end: mint ->
    authenticated request -> obs_callback reads UserAPIKeyAuth -> delivered record
    -> dashboard. The master-key requests the rest of the suite makes carry a
    NULL identity (no key store behind the master key), so a non-null alias here
    is unambiguous attribution to this test's key."""
    # Mint a key bound to a synthetic alias + user + team. _unique keeps all three
    # collision-free across repeated / --keep runs; the alias stays repo-a-shaped
    # (synthetic, per the guardrail) — never a real name/email.
    team_id = _unique("team")
    user_id = _unique("user")
    key_alias = _unique("repo-a")
    _admin_post(
        "/team/new", {"team_id": team_id, "team_alias": team_id, "max_budget": 1000}
    )
    _admin_post(
        "/team/member_add",
        {"team_id": team_id, "member": {"user_id": user_id, "role": "user"}},
    )
    gen = _admin_post(
        "/key/generate",
        {
            "models": ["qwen3-coder"],
            "key_alias": key_alias,
            "user_id": user_id,
            "team_id": team_id,
            "max_budget": 1000,
        },
    )
    key = gen["key"]

    # Drive a request WITH THAT KEY (not the master key) so UserAPIKeyAuth carries
    # our identity into the success hook.
    r = httpx.post(
        GATEWAY + "/v1/chat/completions",
        headers={"Authorization": "Bearer " + key},
        json={
            "model": "qwen3-coder",
            "messages": [{"role": "user", "content": "ping identity"}],
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text

    data = _poll_dash(
        lambda d: any(
            rq.get("key_alias") == key_alias for rq in d.get("requests", [])
        )
    )

    # (a) the per-request row carries the caller's synthetic identity.
    reqs = [
        rq for rq in data.get("requests", []) if rq.get("key_alias") == key_alias
    ]
    assert reqs, (
        "dashboard has no request row carrying the minted key's alias %r: %r"
        % (key_alias, data.get("requests"))
    )
    rq = reqs[0]
    assert rq.get("user_id") == user_id, (
        "request row must carry the key's user_id: %r" % rq
    )
    assert rq.get("team_id") == team_id, (
        "request row must carry the key's team_id: %r" % rq
    )

    # (b) the per-key rollup aggregates that identity's traffic.
    keys = [k for k in data.get("keys", []) if k.get("key_alias") == key_alias]
    assert keys, (
        "dashboard per-key rollup is missing the minted key %r: %r"
        % (key_alias, data.get("keys"))
    )
    k = keys[0]
    assert k.get("user_id") == user_id and k.get("team_id") == team_id, (
        "per-key rollup must carry the key's user+team: %r" % k
    )
    assert k.get("requests") >= 1, "per-key rollup must count the request: %r" % k
    assert k.get("tokens"), "per-key rollup must total the key's tokens: %r" % k


# --- fleet dashboard v2: the control-plane registry, made visible (goal 13) --
#
# Goal 5 built the control-plane registry (e2e/control_plane.py): workbenches
# PUSH heartbeats declaring {warm, in_flight, agent_capable, healthy} per model,
# and it derives per-model aggregates. Goal 13 makes that LIVE on the dashboard:
# the dashboard's /api/fleet endpoint (control-plane-e2e:9400 -> dashboard,
# server-side) is what the Fleet section renders. These tests cover the
# REGISTRY -> DASHBOARD data path end to end: push a heartbeat to the
# control-plane, then assert it surfaces through the dashboard's owned endpoint.
#
# The TEST plays the workbench here (no mockd beats the control-plane in the e2e
# stack — see docker-compose.e2e.yaml) so the fleet state is deterministic. The
# dev stack is where real mockd workbenches beat live (docker-compose.dev.yaml).


def _dash_fleet():
    """The dashboard's fleet data endpoint — a server-side read of the
    control-plane registry, and what the Fleet section renders."""
    r = httpx.get(DASH + "/api/fleet", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _beat(workbench_id, model, **state):
    """Push one heartbeat to the control-plane as `workbench_id`, declaring
    `model` with the given {warm, in_flight, agent_capable, healthy} state."""
    r = httpx.post(
        CTRL + "/heartbeat",
        json={"workbench_id": workbench_id, "models": [{"model": model, **state}]},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _poll_fleet(predicate, timeout=8.0):
    """The dashboard reads the control-plane synchronously per request, so a
    heartbeat is visible on the very next /api/fleet — but poll briefly anyway
    to stay robust to container/scheduling hiccups."""
    deadline = time.time() + timeout
    data = _dash_fleet()
    while time.time() < deadline:
        if predicate(data):
            return data
        time.sleep(0.25)
        data = _dash_fleet()
    return data


def _fleet_model(data, name):
    for m in data.get("models", []):
        if m.get("model") == name:
            return m
    return None


def test_dashboard_fleet_reflects_control_plane_registry():
    """Goal 13: two workbenches heartbeat the SAME model with load; the
    dashboard's fleet endpoint must aggregate them — warm+healthy counts, summed
    in-flight, agent_capable — AND list both workbenches as instances. This is
    the registry -> dashboard data path, asserted on the endpoint the UI
    renders."""
    _beat(
        "wb-alpha",
        "qwen3-coder",
        warm=True,
        in_flight=3,
        agent_capable=True,
        healthy=True,
    )
    _beat(
        "wb-bravo",
        "qwen3-coder",
        warm=True,
        in_flight=2,
        agent_capable=True,
        healthy=True,
    )

    data = _poll_fleet(
        lambda d: (
            d.get("available")
            and (_fleet_model(d, "qwen3-coder") or {}).get("healthy") == 2
        )
    )
    assert data.get("available") is True, (
        "fleet endpoint must be available with the control-plane up: %r" % data
    )

    m = _fleet_model(data, "qwen3-coder")
    assert m, "fleet endpoint has no qwen3-coder model: %r" % data
    assert m["healthy"] == 2, "both healthy instances must aggregate: %r" % m
    assert m["warm"] == 2, "both warm instances must count: %r" % m
    assert m["in_flight"] == 5, "in-flight must sum across instances (3+2): %r" % m
    assert m["agent_capable"] is True, "model is agent-capable if any box is: %r" % m

    # The per-workbench (instance) view — "which box is subscribed, how loaded".
    insts = {
        i["workbench_id"]: i
        for i in data.get("instances", [])
        if i.get("model") == "qwen3-coder"
    }
    assert set(insts) == {"wb-alpha", "wb-bravo"}, (
        "fleet must list both workbenches as instances: %r" % data.get("instances")
    )
    assert insts["wb-alpha"]["in_flight"] == 3, insts["wb-alpha"]
    assert insts["wb-alpha"]["healthy"] is True, insts["wb-alpha"]


def test_dashboard_fleet_surfaces_derived_health():
    """Goal 13: the control-plane DERIVES health (reported_healthy AND fresh); a
    workbench reporting healthy=false must show as unhealthy on the dashboard AND
    be excluded from the model's healthy/warm/in-flight aggregate — proving the
    derived-health signal survives the whole registry -> dashboard path, not just
    the raw counts."""
    _beat("wb-live", "gpt", warm=True, in_flight=1, agent_capable=True, healthy=True)
    _beat("wb-sick", "gpt", warm=True, in_flight=9, agent_capable=True, healthy=False)

    data = _poll_fleet(
        lambda d: d.get("available") and _fleet_model(d, "gpt") is not None
    )
    m = _fleet_model(data, "gpt")
    assert m, "fleet endpoint has no gpt model: %r" % data
    # Only the healthy box counts; the unhealthy one is visible but excluded.
    assert m["healthy"] == 1, "only the healthy instance counts as healthy: %r" % m
    assert m["warm"] == 1, "the unhealthy box's warm slot must not count: %r" % m
    assert m["in_flight"] == 1, (
        "the unhealthy box's in-flight (9) must be excluded from the aggregate: %r" % m
    )
    assert m["instances_total"] == 2, "both boxes are still listed: %r" % m

    insts = {
        i["workbench_id"]: i
        for i in data.get("instances", [])
        if i.get("model") == "gpt"
    }
    assert insts["wb-sick"]["healthy"] is False, (
        "the workbench that reported unhealthy must show unhealthy: %r"
        % insts["wb-sick"]
    )
    assert insts["wb-live"]["healthy"] is True, insts["wb-live"]


# --- spend audit: users, teams, attribution, durability (goal 11b) -----------
# Goal 11 proved the wallet GATES (over-budget / over-limit -> clean 4xx). Goal
# 11b proves the LEDGER: with per-model costs configured (litellm-config.e2e.yaml
# -> qwen3-coder input/output_cost_per_token) a request accrues NONZERO spend
# that LiteLLM attributes to the calling key, its user, and its team, records per
# request in LiteLLM_SpendLogs, and persists in Postgres across a gateway
# restart. These endpoints are the audit surface documented in README "Spend
# audit — who spent what". All of them require the master key.
#
# WHY POLL: spend is buffered in gateway memory and flushed to Postgres on an
# interval (proxy_batch_write_at, default 60s). The DB-backed info/logs endpoints
# only report spend AFTER a flush, so we poll — which also makes the flush our
# durability precondition: once an endpoint reports spend, it's in Postgres, not
# just in memory, so a restart genuinely tests persistence rather than a race.

SPEND_POLL_TIMEOUT = 90.0  # generous: covers the default 60s flush even if the
# proxy_batch_write_at knob is ignored by this build.


def _unique(tag):
    """A per-run-unique id so a --keep stack (or repeated runs) can't cross
    ephemeral rows. pid+ms keeps it collision-free without needing randomness."""
    return "e2e-%s-%d-%d" % (tag, os.getpid(), int(time.time() * 1000))


def _admin_post(path, body):
    r = httpx.post(GATEWAY + path, headers=AUTH, json=body, timeout=TIMEOUT)
    assert r.status_code == 200, "%s failed (%s): %s" % (path, r.status_code, r.text)
    return r.json()


def _admin_get(path):
    r = httpx.get(GATEWAY + path, headers=AUTH, timeout=TIMEOUT)
    assert r.status_code == 200, "%s failed (%s): %s" % (path, r.status_code, r.text)
    return r.json()


def _dig_spend(info):
    """Pull aggregate spend out of a /key|/user|/team info response, tolerating
    the small shape differences between them (top-level `spend`, or nested under
    `info` / `user_info` / `team_info`)."""
    if not isinstance(info, dict):
        return None
    for c in (info, info.get("info"), info.get("user_info"), info.get("team_info")):
        if isinstance(c, dict) and c.get("spend") is not None:
            return float(c["spend"])
    return None


def _provision_team_user_key():
    """Create a team, group a user into it, and mint a key bound to that
    user+team. Returns (team_id, user_id, key). Ids are explicit + unique so the
    audit queries are unambiguous even on a shared/--keep stack. Budgets are large
    (1000 USD) so provisioning never trips the goal-11 gate — this is the LEDGER,
    not the gate."""
    team_id = _unique("team")
    user_id = _unique("user")
    _admin_post(
        "/team/new", {"team_id": team_id, "team_alias": team_id, "max_budget": 1000}
    )
    # Group the user INTO the team (auto-creates the internal-user row). This is
    # the "users can be grouped into teams" half of the goal, made queryable via
    # /team/info.
    _admin_post(
        "/team/member_add",
        {"team_id": team_id, "member": {"user_id": user_id, "role": "user"}},
    )
    gen = _admin_post(
        "/key/generate",
        {
            "models": ["qwen3-coder"],
            "user_id": user_id,
            "team_id": team_id,
            "max_budget": 1000,
        },
    )
    return team_id, user_id, gen["key"]


def _spend_traffic(key, n=2):
    """Send n costed, non-streaming requests on `key` (non-stream so mockd's usage
    block is present and LiteLLM can cost it)."""
    for _ in range(n):
        r = httpx.post(
            GATEWAY + "/v1/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json=_CHAT,
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, "costed request failed: %s" % r.text


def _spend_log_rows_for(user_id):
    """DB-backed per-request ledger (LiteLLM_SpendLogs) filtered to our user.
    /spend/logs reads the table directly, so a row here == it's in Postgres. Try
    the server-side user filter first, fall back to unfiltered + client filter,
    and normalize the list shape either way."""
    for path in ("/spend/logs?user_id=" + user_id, "/spend/logs"):
        r = httpx.get(GATEWAY + path, headers=AUTH, timeout=TIMEOUT)
        if r.status_code != 200:
            continue
        data = r.json()
        rows = (
            data
            if isinstance(data, list)
            else (data.get("data") or data.get("logs") or [])
        )
        rows = [x for x in rows if x.get("user") == user_id]
        if rows:
            return rows
    return []


def _poll(fn, ok, timeout=SPEND_POLL_TIMEOUT):
    """Poll fn() until ok(result) or timeout; return the last result either way so
    the caller's assertion can render the best available value."""
    deadline = time.time() + timeout
    val = fn()
    while time.time() < deadline and not ok(val):
        time.sleep(1.0)
        val = fn()
    return val


def _nonzero(v):
    return isinstance(v, (int, float)) and v > 0


def test_spend_attributed_to_key_user_team():
    """A costed request's spend is attributed to the RIGHT key, user, and team.

    Provision team -> user-in-team -> key (all fresh, all zero-spend), send costed
    traffic, then read the audit surface and assert:
      * a per-request LiteLLM_SpendLogs row carries our user + team + model +
        nonzero spend, hashed to OUR key — per-request attribution tying
        key->user->team on a single row; and
      * /key/info, /user/info, /team/info each report nonzero AGGREGATE spend.
    Because these entities served only this test's traffic, nonzero spend on each
    is unambiguous attribution.
    """
    team_id, user_id, key = _provision_team_user_key()
    _spend_traffic(key, n=2)

    # (1) per-request ledger row (also our "it's in Postgres" gate)
    rows = _poll(lambda: _spend_log_rows_for(user_id), bool)
    assert rows, (
        "no SpendLogs row attributed to user %s within %ss — spend never "
        "accrued/flushed" % (user_id, SPEND_POLL_TIMEOUT)
    )
    row = rows[-1]
    assert row.get("team_id") == team_id, (
        "spend-log row must carry the right team_id: %r" % row
    )
    assert "qwen3-coder" in str(row.get("model", "")), (
        "spend-log row must name the served model: %r" % row
    )
    assert _nonzero(float(row.get("spend") or 0)), (
        "per-request spend must be nonzero (costs are configured): %r" % row
    )
    # Tie the row to OUR key: LiteLLM stores the unsalted sha256 of the key.
    assert row.get("api_key") == hashlib.sha256(key.encode()).hexdigest(), (
        "spend-log row must be attributed to the issuing key's hash: %r" % row
    )

    # (2) aggregate ledgers: key, user, team each show nonzero spend
    key_spend = _dig_spend(
        _poll(
            lambda: _admin_get("/key/info?key=" + key),
            lambda i: _nonzero(_dig_spend(i)),
        )
    )
    user_spend = _dig_spend(
        _poll(
            lambda: _admin_get("/user/info?user_id=" + user_id),
            lambda i: _nonzero(_dig_spend(i)),
        )
    )
    team_spend = _dig_spend(
        _poll(
            lambda: _admin_get("/team/info?team_id=" + team_id),
            lambda i: _nonzero(_dig_spend(i)),
        )
    )
    assert _nonzero(key_spend), "key ledger must show nonzero spend: %r" % key_spend
    assert _nonzero(user_spend), "user ledger must show nonzero spend: %r" % user_spend
    assert _nonzero(team_spend), "team ledger must show nonzero spend: %r" % team_spend


def _restart_gateway_and_wait():
    """Restart the gateway CONTAINER (not the db) and block until it's healthy
    again. Gated on E2E_ALLOW_RESTART because it shells out to `docker` — run.sh
    sets it, so the durability proof runs in the arbiter; a bare `pytest` against a
    manual or remote stack skips rather than killing someone's gateway."""
    if os.environ.get("E2E_ALLOW_RESTART") != "1":
        pytest.skip("restart durability needs E2E_ALLOW_RESTART=1 (set by run.sh)")
    container = os.environ.get("E2E_LITELLM_CONTAINER", "litellm-e2e")
    subprocess.run(
        ["docker", "restart", container], check=True, capture_output=True, timeout=120
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if httpx.get(GATEWAY + "/health/liveliness", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise AssertionError("gateway did not become healthy after restart")


def test_spend_survives_gateway_restart():
    """Durability: spend written to Postgres must still be there after the gateway
    process restarts (in-memory buffers cleared, caches cold). This answers the
    open persistence question for good — issued keys AND their spend live in
    Postgres, not gateway memory.

    Provision + spend, poll until the ledger is confirmed IN THE DB (a SpendLogs
    row plus nonzero user/team aggregates — all DB reads), snapshot it, restart the
    gateway, then re-read and assert the ledger is intact (>= snapshot, still
    nonzero) and the issued key still exists.
    """
    team_id, user_id, key = _provision_team_user_key()
    _spend_traffic(key, n=2)

    rows_before = _poll(lambda: _spend_log_rows_for(user_id), bool)
    assert rows_before, (
        "spend never reached Postgres within %ss; cannot test durability"
        % SPEND_POLL_TIMEOUT
    )
    user_before = _dig_spend(
        _poll(
            lambda: _admin_get("/user/info?user_id=" + user_id),
            lambda i: _nonzero(_dig_spend(i)),
        )
    )
    team_before = _dig_spend(
        _poll(
            lambda: _admin_get("/team/info?team_id=" + team_id),
            lambda i: _nonzero(_dig_spend(i)),
        )
    )
    assert _nonzero(user_before), (
        "user spend not persisted pre-restart: %r" % user_before
    )
    assert _nonzero(team_before), (
        "team spend not persisted pre-restart: %r" % team_before
    )

    _restart_gateway_and_wait()  # skips here if E2E_ALLOW_RESTART != 1

    # Cold restart: the gateway must serve the SAME ledger straight from Postgres.
    rows_after = _spend_log_rows_for(user_id)
    assert rows_after, (
        "SpendLogs row for %s vanished after restart — spend was not durable" % user_id
    )
    assert rows_after[-1].get("team_id") == team_id, (
        "restored spend-log row lost its team attribution: %r" % rows_after[-1]
    )
    user_after = _dig_spend(_admin_get("/user/info?user_id=" + user_id))
    team_after = _dig_spend(_admin_get("/team/info?team_id=" + team_id))
    key_after = _dig_spend(_admin_get("/key/info?key=" + key))  # key row survived too
    assert _nonzero(user_after) and user_after >= user_before, (
        "user spend must persist across restart: before=%r after=%r"
        % (user_before, user_after)
    )
    assert _nonzero(team_after) and team_after >= team_before, (
        "team spend must persist across restart: before=%r after=%r"
        % (team_before, team_after)
    )
    assert _nonzero(key_after), (
        "issued key + its spend must persist across restart: %r" % key_after
    )
