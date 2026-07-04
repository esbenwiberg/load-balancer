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

import json
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:4000")
MOCKD = os.environ.get("MOCKD_URL", "http://localhost:9100")
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
    yield
    httpx.post(MOCKD + "/__reset", timeout=TIMEOUT)


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
