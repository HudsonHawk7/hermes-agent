"""HTS-030: a task can forbid the main-agent last resort.

A cloud-pinned aux call (tts_audio_tags on xai, title generation) must not
fall through onto the local GPU that is serving the chat. Missing or true
keeps today's fallback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.auxiliary_client import call_llm
from agent.auxiliary_task_config import session_auxiliary_scope


class _Timeout(Exception):
    """Name carries Timeout so _is_timeout_error treats it as a full-budget timeout."""


def _ok_response(text: str):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


def _drive(task_cfg, *, main_client):
    """Time out an explicit xai pin and let the real last-resort path run."""
    primary = MagicMock()
    primary.base_url = "https://api.x.ai/v1"
    primary.chat.completions.create.side_effect = _Timeout("Request timed out.")

    def _resolve(provider, model=None, **kwargs):
        if provider == "ollama-local":
            return main_client, "qwen3.8:27b"
        raise AssertionError(f"unexpected provider client build: {provider}")

    with session_auxiliary_scope({"tts_audio_tags": task_cfg}), \
         patch("agent.auxiliary_client._resolve_task_provider_model",
               return_value=("xai", "grok-4.3", "https://api.x.ai/v1", "test-key", None)), \
         patch("agent.auxiliary_client._get_cached_client",
               return_value=(primary, "grok-4.3")), \
         patch("agent.auxiliary_client._read_main_provider", return_value="ollama-local"), \
         patch("agent.auxiliary_client._read_main_model", return_value="qwen3.8:27b"), \
         patch("agent.auxiliary_client._is_provider_unhealthy", return_value=False), \
         patch("agent.auxiliary_client._custom_health_base_url", return_value=""), \
         patch("agent.auxiliary_client._transient_retry_count", return_value=0), \
         patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve) as built:
        try:
            result = call_llm(
                task="tts_audio_tags",
                messages=[{"role": "user", "content": "tag this"}],
            )
        except _Timeout:
            result = None
        return result, built, main_client, primary


def test_flagged_task_timeout_never_reaches_main_client(caplog):
    """RED on base: allow_main_fallback false must not build the main client."""
    main = MagicMock()
    main.chat.completions.create.return_value = _ok_response("should not run")
    with caplog.at_level("INFO"):
        result, built, main, primary = _drive(
            {"provider": "xai", "model": "grok-4.3", "allow_main_fallback": False},
            main_client=main,
        )

    assert result is None
    assert primary.chat.completions.create.called
    assert built.call_count == 0, "main client was built after a flagged timeout"
    assert main.chat.completions.create.call_count == 0
    log = caplog.text
    assert "allow_main_fallback=false" in log
    assert "falling back to main agent model" not in log


def test_string_false_also_skips_main_client():
    main = MagicMock()
    result, built, main, _primary = _drive(
        {"provider": "xai", "model": "grok-4.3", "allow_main_fallback": "false"},
        main_client=main,
    )
    assert result is None
    assert built.call_count == 0
    assert main.chat.completions.create.call_count == 0


def test_unflagged_task_timeout_still_falls_back_to_main(caplog):
    """Missing key keeps today's last resort."""
    main = MagicMock()
    main.base_url = "http://127.0.0.1:11434/v1"
    main.chat.completions.create.return_value = _ok_response("local fallback")
    with caplog.at_level("INFO"):
        result, built, main, primary = _drive(
            {"provider": "xai", "model": "grok-4.3"},
            main_client=main,
        )

    assert result is not None
    assert result.choices[0].message.content == "local fallback"  # type: ignore[union-attr]
    assert primary.chat.completions.create.called
    assert built.call_count == 1
    assert built.call_args.kwargs["provider"] == "ollama-local"
    assert main.chat.completions.create.called
    assert "falling back to main agent model" in caplog.text
    assert "main-agent(ollama-local)" in caplog.text


def test_explicit_true_still_falls_back():
    main = MagicMock()
    main.chat.completions.create.return_value = _ok_response("allowed")
    result, built, main, _primary = _drive(
        {"provider": "xai", "model": "grok-4.3", "allow_main_fallback": True},
        main_client=main,
    )
    assert result is not None
    assert result.choices[0].message.content == "allowed"  # type: ignore[union-attr]
    assert built.call_count == 1
    assert main.chat.completions.create.called
