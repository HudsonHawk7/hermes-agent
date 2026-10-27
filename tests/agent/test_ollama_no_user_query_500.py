"""Ollama's Qwen-template 500 "no user query found in messages": a server-side prune
(kanban t_f9c4f6cf — Ahsoka's ``qwen3.8:27b``, HTTP 500 on every board call).

Ollama trims the rendered transcript from the FRONT to fit ``num_ctx`` (its guard protects
only the LAST message; in a tool loop the last message is a tool result), so the user turn
that started the loop is the one that goes, and Qwen's template backward user-query scan
then raises — served as **HTTP 500** ("no user query found in messages", measured on
Ollama 0.40.9). The identical raise served as **HTTP 400** (vLLM) is already classified
format_error by the invalid-message-body table (#58753 family).

Pre-fix, the 500 read as a transient ``server_error`` and burned the retry ladder
(~200 calls / ~20 minutes on card t_85dda4cf, 10/26) on a deterministically unfixable
request (measured by replaying the recorded failing body to a live Ollama). The recorded
failing payload still contained its user turn — Hermes never lost it; the server pruned it.

Contract:
- the 500 is classified ``context_overflow`` (compression recovery; the compressor's
  zero-user guard keeps a user turn on the compressed replay) when the OUTBOUND transcript
  still carries its user turn;
- it is classified ``format_error`` (fail fast — no compression, no retry ladder) when the
  outbound transcript itself has NO user turn: that is our own request bug, and compression
  cannot invent the missing turn;
- recovery for the prune skips the "another request holds the slot" wait-branch
  (#114644) — the server just told us its window is below the request — and compresses
  against a window floor bounded by the request the server actually rejected (in memory,
  never persisted: a floor, not a measured limit);
- a server that QUOTES its own limit (``maximum context length is 128000 tokens``) keeps
  its limit; the prune floor never overrides it.

- the aux summarizer being unavailable (aux window below the 64K minimum: any small
  local model) never kills the worker mid-recovery: the context-length and 413 paths
  degrade to a model-free tool-payload trim (user turn + pairing intact, rounds capped)
  and end with an honest typed failure when they cannot fit;
Windows note: the main-loop test imports ``run_agent`` like ``test_413_compression`` does;
run the file after a module that imports it (or with that file) on this host.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import conversation_loop  # (import at collection: its chain activates
# hermes_bootstrap, which reads the Hermes home — that read is legal here but refused once
# the home-I/O guard arms inside a test; test_413_compression.py relies on the same layout)
from run_agent import AIAgent  # (same collection-time reason)
from agent.error_classifier import (
    FailoverReason,
    RETRYABLE_CLIENT_REASONS,
    classify_api_error,
    transcript_has_user_turn,
)
from agent.turn_overflow import recover_from_overflow

_OLLAMA_BODY = {"error": {"code": 500, "message": "no user query found in messages", "type": "api_error"}}


class _MockAPIError(Exception):
    """Simulates an OpenAI SDK APIStatusError whose body is the server's JSON."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


def _ollama_500() -> Exception:
    return _MockAPIError(
        "Error code: 500 - {'error': {'message': 'no user query found in messages', 'code': 500}}",
        status_code=500,
        body=dict(_OLLAMA_BODY),
    )


# ── Classification: the Ollama 500 prune is an overflow, not a server flake ──


def test_500_with_user_turn_classifies_as_overflow_for_compression():
    classified = classify_api_error(
        _ollama_500(), provider="ollama", model="qwen3.8:27b",
        approx_tokens=36000, context_length=200000, num_messages=5,
        base_url="http://127.0.0.1:11434/v1", has_user_turn=True,
    )
    assert classified.reason == FailoverReason.context_overflow
    assert classified.should_compress is True
    assert classified.retryable is True  # compression runs, then the request retries
    assert classified.error_context.get("user_turn_pruned") is True


def test_500_without_user_turn_fails_fast_as_format_error():
    classified = classify_api_error(
        _ollama_500(), provider="ollama", model="qwen3.8:27b",
        approx_tokens=36000, context_length=200000, num_messages=3,
        base_url="http://127.0.0.1:11434/v1", has_user_turn=False,
    )
    assert classified.reason == FailoverReason.format_error
    assert classified.should_compress is False
    assert classified.retryable is False
    assert classified.reason.value not in RETRYABLE_CLIENT_REASONS


def test_500_with_unknown_transcript_defaults_to_overflow():
    classified = classify_api_error(
        _ollama_500(), provider="ollama", model="qwen3.8:27b",
        approx_tokens=36000, context_length=200000, num_messages=3,
        base_url="http://127.0.0.1:11434/v1",
    )
    assert classified.reason == FailoverReason.context_overflow


def test_400_keeps_format_error_classification():
    """The vLLM 400 shape (#58753) must keep its existing fail-fast verdict."""
    classified = classify_api_error(
        _MockAPIError("Error code: 400 - no user query found in messages", status_code=400,
                      body={"message": "no user query found in messages"}),
        provider="", model="", approx_tokens=0, context_length=200000, num_messages=2,
    )
    assert classified.reason == FailoverReason.format_error
    assert classified.should_compress is False


def test_transcript_has_user_turn_helpers():
    assert transcript_has_user_turn([{"role": "system"}, {"role": "user", "content": "go"}]) is True
    # Tool messages never count — Qwen templates scan user roles only.
    assert transcript_has_user_turn([{"role": "tool", "content": "ok"}]) is False
    assert transcript_has_user_turn(None) is None
    assert transcript_has_user_turn([]) is False


def test_500_server_quoted_limit_is_not_flagged_as_prune():
    """A relay that quotes its own limit keeps it; the prune floor never overrides it."""
    classified = classify_api_error(
        _MockAPIError("prompt is too long: 233153 tokens > 200000", status_code=500,
                      body={"error": {"message": "prompt is too long: 233153 tokens > 200000"}}),
        provider="ollama", model="qwen3.8:27b", approx_tokens=36000,
        context_length=200000, num_messages=3, base_url="http://127.0.0.1:11434/v1",
        has_user_turn=True,
    )
    assert classified.reason == FailoverReason.context_overflow
    assert classified.error_context.get("user_turn_pruned") is None


# ── Recovery: a server-pruned request compresses; it does not wait on a slot ──


def _recovery_transcript():
    return [
        {"role": "user", "content": "start the task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "kanban_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "board contents"},
    ]


def _recovery_agent():
    agent = MagicMock()
    agent.base_url = "http://127.0.0.1:11434/v1"
    agent.model = "qwen3.8:27b"
    agent.provider = "ollama"
    agent.tools = None
    agent.max_tokens = 8000
    agent.log_prefix = "test ▸ "
    agent.context_compressor = MagicMock()
    agent.context_compressor.context_length = 200000
    return agent


def test_pruned_500_recovery_compresses_instead_of_waiting_on_the_slot():
    agent = _recovery_agent()
    transcript = _recovery_transcript()
    _retry = SimpleNamespace(restart_with_compressed_messages=False)

    def _shrink(before, system_message, **_kw):
        return ([{"role": "user", "content": "summary of the loop so far"}], "compressed prompt")

    with (
        patch("agent.turn_overflow.context_compression_timed_out", return_value=False),
        patch("agent.turn_overflow.compression_skipped_due_to_lock", return_value=False),
        patch("agent.turn_overflow.compression_blocked_transiently", return_value=False),
        patch("agent.conversation_compression.conversation_history_after_compression", side_effect=lambda agent_, m, h: h),
        patch.object(agent, "_compress_context", side_effect=_shrink) as mock_compress,
    ):
        verdict = recover_from_overflow(
            agent, _ollama_500(), classify_api_error(
                _ollama_500(), provider="ollama", model="qwen3.8:27b",
                approx_tokens=36000, context_length=200000, num_messages=3,
                base_url=agent.base_url, has_user_turn=True,
            ), _retry, status_code=500,
            error_msg="no user query found in messages", wrapped_output_cap_budget=None,
            messages=list(transcript), api_messages=list(transcript), system_message="system",
            active_system_prompt="system", conversation_history=[],
            approx_tokens=36000, compression_attempts=0,
            max_compression_attempts=3, api_call_count=1, effective_task_id="t",
        )

    assert verdict.action == "break"
    assert _retry.restart_with_compressed_messages is True
    assert mock_compress.call_count == 1
    # The window floor is adopted in memory: the served window sits below the request
    # the server just failed to render (request + reserved output, capped at the old window).
    kwargs = agent.context_compressor.update_model.call_args.kwargs
    old_window = 200000
    assert 0 < kwargs["context_length"] < old_window


def test_pruned_500_recovery_keeps_a_server_quoted_limit():
    agent = _recovery_agent()
    transcript = _recovery_transcript()
    _retry = SimpleNamespace(restart_with_compressed_messages=False)
    too_long = ("This model's maximum context length is 128000 tokens. "
                "However, you requested 233153 tokens in the messages.")
    err = _MockAPIError(too_long, status_code=500, body={"error": {"message": too_long}})

    with (
        patch("agent.turn_overflow.context_compression_timed_out", return_value=False),
        patch("agent.turn_overflow.compression_skipped_due_to_lock", return_value=False),
        patch("agent.turn_overflow.compression_blocked_transiently", return_value=False),
        patch("agent.conversation_compression.conversation_history_after_compression", side_effect=lambda agent_, m, h: h),
        patch.object(agent, "_compress_context", return_value=([{"role": "user", "content": "s"}], "p")),
        patch("agent.model_metadata.save_provider_context_length") as mock_save,
    ):
        verdict = recover_from_overflow(
            agent, err, classify_api_error(
                err, provider="ollama", model="qwen3.8:27b", approx_tokens=36000,
                context_length=200000, num_messages=3, base_url=agent.base_url,
                has_user_turn=True,
            ), _retry, status_code=500,
            error_msg=too_long, wrapped_output_cap_budget=None,
            messages=list(transcript), api_messages=list(transcript), system_message="system",
            active_system_prompt="system", conversation_history=[],
            approx_tokens=36000, compression_attempts=0,
            max_compression_attempts=3, api_call_count=1, effective_task_id="t",
        )

    assert verdict.action == "break"
    saved = agent.context_compressor.update_model.call_args_list
    assert saved and saved[-1].kwargs["context_length"] == 128000
    assert mock_save.call_count == 1


# ── The kanban-path turn: 500 → compress → retry that keeps a user turn ──


def _mock_response(content="answered", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls,
                          reasoning_content=None, reasoning=None)
    resp = SimpleNamespace(model="test/model")
    resp.choices = [SimpleNamespace(message=msg, finish_reason=finish_reason)]
    resp.usage = None
    return resp


def _loop_agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3.8:27b",
            provider="ollama",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = True
    agent.save_trajectories = False
    return agent


def test_ollama_500_compression_recovers_the_turn():
    agent = _loop_agent()
    # First pass: the 500 arrives with a user turn and compresses instead of climbing
    # the ladder; the compressed replay is answered on its first attempt. (Pre-fix the
    # ladder consumed the success on attempt 2 and compression never ran.)
    ollama_500 = _ollama_500()
    agent.client.chat.completions.create.side_effect = [
        ollama_500, _mock_response("Success after compression")]
    prefill = [
        {"role": "user", "content": "earlier work" * 200},
        {"role": "assistant", "content": "earlier answer" * 200},
    ]

    with (
        patch.object(agent, "_compress_context") as mock_compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        mock_compress.return_value = ([{"role": "user", "content": "hello"}], "compressed prompt")
        result = agent.run_conversation("hello", conversation_history=prefill)

    assert mock_compress.call_count == 1
    assert result.get("completed") is True, result
    assert result.get("final_response") == "Success after compression", result
    # The compressed replay keeps a user turn — the strict template requires one.
    retry_messages = agent.client.chat.completions.create.call_args_list[-1].kwargs["messages"]
    assert retry_messages and any(m.get("role") == "user" for m in retry_messages if isinstance(m, dict))


@pytest.fixture(autouse=True)
def _no_compression_sleep(monkeypatch):
    """Short-circuit sleeps/backoffs: tests assert behavior, not timing."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    from agent import retry_utils as _retry_utils

    monkeypatch.setattr(_retry_utils, "jittered_backoff", lambda *a, **k: 0.0)


# ── Sub-64K aux: the summarizer is unavailable; recovery must survive, not die ──


_AUX_INFEASIBLE = ValueError(
    "Auxiliary compression model qwen3.8:27b has a context window of 37,168 tokens, which is "
    "below the minimum 64,000 required by Hermes Agent. Choose a compression model with at "
    "least 64K context (set auxiliary.compression.model in config.yaml)."
)


def _fake_estimate(msgs, **kwargs):
    """Rough stand-in for both token estimators, deterministic on content length."""
    return sum(len(str(m.get("content") or "")) // 4 + 8 for m in msgs if isinstance(m, dict))


def _prune_recovery_call(agent, transcript, attempts=0, max_attempts=3):
    """One recovery pass on a server-pruned 500 with an INFEASIBLE aux summarizer."""
    _retry = SimpleNamespace(restart_with_compressed_messages=False)
    with (
        patch("agent.model_metadata.estimate_messages_tokens_rough", _fake_estimate),
        patch("agent.model_metadata.estimate_request_tokens_rough", _fake_estimate),
        patch("agent.turn_overflow.context_compression_timed_out", return_value=False),
        patch("agent.turn_overflow.compression_skipped_due_to_lock", return_value=False),
        patch("agent.turn_overflow.compression_blocked_transiently", return_value=False),
        patch("agent.conversation_compression.conversation_history_after_compression", side_effect=lambda agent_, m, h: h),
        patch.object(agent, "_compress_context", side_effect=_AUX_INFEASIBLE) as mock_compress,
    ):
        verdict = recover_from_overflow(
            agent, _ollama_500(), classify_api_error(
                _ollama_500(), provider="ollama", model="qwen3.8:27b",
                approx_tokens=150000, context_length=256000, num_messages=3,
                base_url=agent.base_url, has_user_turn=True,
            ), _retry, status_code=500,
            error_msg="no user query found in messages", wrapped_output_cap_budget=None,
            messages=list(transcript), api_messages=list(transcript), system_message="system",
            active_system_prompt="system", conversation_history=[],
            approx_tokens=150000, compression_attempts=attempts,
            max_compression_attempts=max_attempts, api_call_count=1, effective_task_id="t",
        )
    return verdict, mock_compress, _retry


def test_pruned_500_recovery_survives_infeasible_aux_via_offline_tool_trim():
    """The measured board crash (t_6d5e44ce): 500-prune -> compression -> the lazy aux
    feasibility probe hard-raises (37,168 < the 64K summarizer minimum) and the ValueError
    killed the managed worker. Recovery must degrade to the model-free tool trim, keep the
    user turn and pairing, and still signal the replay."""
    agent = _recovery_agent()
    agent.context_compressor.context_length = 256000
    agent.context_compressor.update_model = MagicMock(
        side_effect=lambda **kw: setattr(agent.context_compressor, "context_length", kw["context_length"])
    )
    big = "row" * 200000  # ~600K chars: a board dump that blows the 37K window
    transcript = [
        {"role": "user", "content": "inspect the board"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "kanban_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": big},
    ]

    verdict, mock_compress, _retry = _prune_recovery_call(agent, transcript)

    assert verdict.action == "break"
    assert _retry.restart_with_compressed_messages is True
    assert mock_compress.call_count == 1
    msgs = verdict.messages
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "inspect the board"
    assert msgs[1]["tool_calls"], "assistant tool_calls survived"
    assert msgs[2]["tool_call_id"] == "c1", "tool pairing survived"
    trimmed = msgs[2]["content"]
    assert len(trimmed) < len(big) and "omitted" in trimmed
    assert trimmed.startswith(big[:80]) and trimmed.endswith(big[-80:]), trimmed[:120]


def test_pruned_500_recovery_with_infeasible_aux_and_nothing_to_trim_still_fails_honestly():
    """No oversized tool payloads -> trim declines. The prune floor still earns one replay
    per pass, and with the aux still infeasible the loop repeats until the count_attempt
    cap ends in the typed max-attempts failure. Never an exception, no transcript mutation."""
    agent = _recovery_agent()
    transcript = [
        {"role": "user", "content": "start the task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
         "function": {"name": "kanban_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "small"},
    ]
    for attempt in range(3):
        verdict, _mc, _retry = _prune_recovery_call(agent, transcript, attempts=attempt)
        assert verdict.action == "break", (attempt, verdict)
    final, _mc, _retry = _prune_recovery_call(agent, transcript, attempts=3)
    assert final.action == "return"
    result = final.result or {}
    assert result.get("failed") is True
    assert result.get("completed") is False
    assert "grown too long for" in (result.get("final_response") or "").lower()
    assert len(transcript[2]["content"]) == 5, "transcript untouched"
