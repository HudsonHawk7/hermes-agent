"""compression.summary_max_tokens is an opt-in wire cap on the compression summary call only.

Unset/null means uncapped (today's default). A positive int is sent as max_tokens on the
summary auxiliary call. Chat turns use agent.max_tokens and never pick up this setting.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.agent_init import _parse_compression_config
from agent.context_compressor import ContextCompressor
from hermes_cli.config import DEFAULT_CONFIG


def _agent():
    return SimpleNamespace(
        model="m", provider="openrouter", api_mode="chat_completions", quiet_mode=True,
    )


def _msgs():
    return [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "more"},
        {"role": "assistant", "content": "done"},
    ]


class _Resp:
    class _Choice:
        class _Msg:
            content = "[CONTEXT SUMMARY]: ok"

        message = _Msg()

    choices = [_Choice()]


def test_default_config_leaves_summary_max_tokens_uncapped():
    assert DEFAULT_CONFIG["compression"]["summary_max_tokens"] is None
    cs = _parse_compression_config(_agent(), {})
    assert cs.summary_max_tokens is None


def test_parse_summary_max_tokens_null_stays_uncapped_and_positive_int_is_kept():
    assert _parse_compression_config(
        _agent(), {"compression": {"summary_max_tokens": None}},
    ).summary_max_tokens is None
    assert _parse_compression_config(
        _agent(), {"compression": {"summary_max_tokens": 512}},
    ).summary_max_tokens == 512


def test_summary_call_omits_max_tokens_when_summary_max_tokens_unset():
    seen = {}

    def fake_call_llm(**kwargs):
        seen["kwargs"] = dict(kwargs)
        return _Resp()

    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        c = ContextCompressor(model="test", quiet_mode=True, max_tokens=9999)
    assert c.summary_max_tokens is None

    with patch("agent.context_compressor.call_llm", side_effect=fake_call_llm):
        summary = c._generate_summary(_msgs())

    assert summary is not None
    assert seen["kwargs"].get("task") == "compression"
    assert "max_tokens" not in seen["kwargs"]
    # The compressor's max_tokens is the MAIN chat output reservation for threshold math,
    # not a summary wire cap — it must not leak onto the compression call.
    assert c.max_tokens == 9999


def test_summary_call_sends_max_tokens_when_summary_max_tokens_is_set():
    seen = {}

    def fake_call_llm(**kwargs):
        seen["kwargs"] = dict(kwargs)
        return _Resp()

    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        c = ContextCompressor(model="test", quiet_mode=True, summary_max_tokens=800)

    with patch("agent.context_compressor.call_llm", side_effect=fake_call_llm):
        summary = c._generate_summary(_msgs())

    assert summary is not None
    assert seen["kwargs"].get("task") == "compression"
    assert seen["kwargs"].get("max_tokens") == 800


def test_chat_turn_hook_uses_agent_max_tokens_not_summary_cap():
    """Chat turns report agent.max_tokens to pre_api_request; compressor.summary_max_tokens
    is never consulted on that path."""
    from agent import turn_api_request

    captured = {}

    def fake_invoke(name, **kwargs):
        if name == "pre_api_request":
            captured.update(kwargs)

    agent = SimpleNamespace(
        max_tokens=None,
        context_compressor=SimpleNamespace(summary_max_tokens=777),
        session_id="s",
        platform="cli",
        model="m",
        provider="p",
        base_url="",
        api_mode="chat_completions",
        tools=[],
        _api_request_payload_for_hook=lambda payload: payload,
    )
    with patch("hermes_cli.lifecycle.has_hook", return_value=True), patch(
        "hermes_cli.lifecycle.invoke_hook", side_effect=fake_invoke,
    ):
        turn_api_request._fire_pre_api_request_hook(
            agent,
            api_kwargs={"messages": [{"role": "user", "content": "hi"}]},
            api_messages=[{"role": "user", "content": "hi"}],
            _llm_middleware_trace=[],
            messages=[{"role": "user", "content": "hi"}],
            original_user_message="hi",
            approx_tokens=10,
            total_chars=2,
            retry_count=0,
            api_call_count=1,
            api_request_id="r",
            api_start_time=0.0,
            effective_task_id=None,
            turn_id="t",
        )

    assert captured.get("max_tokens") is None
    assert captured.get("max_tokens") != 777


def _wire_max(kwargs: dict) -> int | None:
    """OpenAI-compat wire may use max_tokens or max_completion_tokens."""
    if "max_tokens" in kwargs:
        return kwargs["max_tokens"]
    if "max_completion_tokens" in kwargs:
        return kwargs["max_completion_tokens"]
    return None


@pytest.mark.parametrize(
    "provider",
    ["custom", "ollama"],
)
def test_build_call_kwargs_forwards_summary_cap_on_ollama_custom_compression(provider):
    """Built request (not just call_llm kwargs) must carry the opt-in cap on Ollama.

    Architect's measured 18,650-token summary used model.provider=ollama → custom alias
    and base http://127.0.0.1:11434/v1. _forwards_max_tokens used to drop max_tokens on
    that route even when task=compression set it explicitly.
    """
    from agent.auxiliary_client import _build_call_kwargs

    msgs = [{"role": "user", "content": "summarize"}]
    base = "http://127.0.0.1:11434/v1"

    capped = _build_call_kwargs(
        provider=provider,
        model="architect:v8-9b",
        messages=msgs,
        max_tokens=800,
        base_url=base,
        task="compression",
    )
    assert _wire_max(capped) == 800

    uncapped = _build_call_kwargs(
        provider=provider,
        model="architect:v8-9b",
        messages=msgs,
        max_tokens=None,
        base_url=base,
        task="compression",
    )
    assert _wire_max(uncapped) is None
    assert "max_tokens" not in uncapped
    assert "max_completion_tokens" not in uncapped


def test_build_call_kwargs_does_not_forward_cap_for_non_compression_ollama_task():
    """Compression-only exception: other aux tasks on Ollama/custom stay on the omit list."""
    from agent.auxiliary_client import _build_call_kwargs

    kwargs = _build_call_kwargs(
        provider="custom",
        model="architect:v8-9b",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=800,
        base_url="http://127.0.0.1:11434/v1",
        task="vision",
    )
    assert _wire_max(kwargs) is None
