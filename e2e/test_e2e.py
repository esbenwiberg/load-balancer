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

import httpx
import pytest

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:4000")
MOCKD = os.environ.get("MOCKD_URL", "http://localhost:9100")
KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-e2e-master-test-key")

AUTH = {"Authorization": "Bearer " + KEY}
TIMEOUT = 30.0


@pytest.fixture(autouse=True)
def _reset_mockd():
    """Clear all injected faults before each test so tests can't leak state."""
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
