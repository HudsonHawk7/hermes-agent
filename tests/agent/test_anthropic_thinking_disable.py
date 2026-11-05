"""'Thinking off' on the native Anthropic Messages wire.

Adaptive Claude models (4.6+) think by DEFAULT.  Omitting the ``thinking``
parameter is therefore NOT a disable — it leaves the upstream default in
place and the model keeps thinking, which is exactly what a user who turned
thinking off is trying to stop paying for.  The disable has to be sent:

    thinking: {"type": "disabled"}

Reasoning-mandatory families (claude-fable) reject that with an HTTP 400
("Thinking is mandatory for this model"), so they keep the omission — a
silently-ignored disable is a much better failure than a dead turn.

Legacy manual-thinking Claude (<= 4.5) needs no disable at all: thinking is
opt-in there via ``budget_tokens``, so sending nothing already means off.

Sibling contract on the chat_completions wire: hermes-agent#90412.
"""

from __future__ import annotations

import pytest

from agent.anthropic_adapter import build_anthropic_kwargs

MESSAGES = [{"role": "user", "content": "hello"}]

# Portal ids and their bare Anthropic equivalents: the disable verdict is a
# property of the model family, not of which route serves it.
ADAPTIVE_DISABLEABLE = [
    "anthropic/claude-opus-5",
    "claude-opus-4-6",
]


def _kwargs(model: str, reasoning_config: dict | None, **extra):
    return build_anthropic_kwargs(
        model=model,
        messages=MESSAGES,
        tools=None,
        max_tokens=4096,
        reasoning_config=reasoning_config,
        **extra,
    )


class TestThinkingOffIsSentExplicitly:
    @pytest.mark.parametrize("model", ADAPTIVE_DISABLEABLE)
    def test_adaptive_models_receive_an_explicit_disable(self, model: str) -> None:
        """The whole point: omission would leave thinking ON for these."""
        kwargs = _kwargs(model, {"enabled": False})
        assert kwargs["thinking"] == {"type": "disabled"}

    @pytest.mark.parametrize("model", ADAPTIVE_DISABLEABLE)
    def test_disable_carries_no_effort_dial(self, model: str) -> None:
        """``output_config.effort`` describes how hard to think — meaningless
        alongside a disable, and it is what the enable path sets."""
        kwargs = _kwargs(model, {"enabled": False, "effort": "high"})
        assert kwargs["thinking"] == {"type": "disabled"}
        assert "output_config" not in kwargs

    @pytest.mark.parametrize("model", ["anthropic/claude-fable-5", "claude-opus-5-5", "anthropic/claude-opus-5.5"])
    def test_mandatory_thinking_models_keep_the_omission(self, model: str) -> None:
        """Fable and Opus 5.5 answer a disable with HTTP 400, so don't send one."""
        kwargs = _kwargs(model, {"enabled": False})
        assert "thinking" not in kwargs

    @pytest.mark.parametrize("model", ["claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"])
    def test_between_tools_is_the_off_switch_where_disabled_400s(self, model: str) -> None:
        """Sonnet 5.5 400s on ``disabled``; its documented lowest setting is ``between_tools``.
        Sonnet 5 still takes the plain disable."""
        assert _kwargs(model, {"enabled": False})["thinking"] == {"type": "between_tools"}
        assert _kwargs("claude-sonnet-5", {"enabled": False})["thinking"] == {"type": "disabled"}

    def test_legacy_manual_thinking_models_keep_the_omission(self) -> None:
        """Pre-4.6 thinking is opt-in via budget_tokens: absence IS off."""
        kwargs = _kwargs("claude-sonnet-4-5", {"enabled": False})
        assert "thinking" not in kwargs

    def test_haiku_keeps_the_omission(self) -> None:
        """Haiku is legacy-manual, so absence already means off."""
        kwargs = _kwargs("anthropic/claude-haiku-4.5", {"enabled": False})
        assert "thinking" not in kwargs

    def test_kimi_keeps_its_documented_omission(self) -> None:
        """Kimi speaks the adaptive contract but is out of scope here (#13848)."""
        kwargs = _kwargs(
            "kimi-k2.5", {"enabled": False}, base_url="https://api.kimi.com/coding"
        )
        assert "thinking" not in kwargs


class TestEnablePathIsUnchanged:
    """The disable branch must not disturb the thinking-ON contract."""

    def test_adaptive_enable_still_sends_adaptive_plus_effort(self) -> None:
        kwargs = _kwargs("anthropic/claude-opus-5", {"enabled": True, "effort": "high"})
        assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert kwargs["output_config"] == {"effort": "high"}

    def test_mandatory_model_still_thinks_when_asked_to(self) -> None:
        kwargs = _kwargs("anthropic/claude-fable-5", {"enabled": True, "effort": "max"})
        assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert kwargs["output_config"] == {"effort": "max"}

    def test_legacy_enable_still_sends_budget_tokens(self) -> None:
        kwargs = _kwargs("claude-sonnet-4-5", {"enabled": True, "effort": "high"})
        assert kwargs["thinking"]["type"] == "enabled"
        assert kwargs["thinking"]["budget_tokens"] > 0

    def test_haiku_still_never_gets_thinking_on_the_enable_path(self) -> None:
        kwargs = _kwargs("anthropic/claude-haiku-4.5", {"enabled": True, "effort": "high"})
        assert "thinking" not in kwargs

    def test_haiku_55_thinks_adaptively(self) -> None:
        """Haiku 5.5 is an adaptive-thinking model (Anthropic/Bedrock model tables); a blanket 'haiku'
        skip sent it no thinking at all."""
        kwargs = _kwargs("anthropic/claude-haiku-5.5", {"enabled": True, "effort": "high"})
        assert kwargs["thinking"] == {"type": "adaptive", "display": "summarized"}

    def test_no_reasoning_config_sends_no_thinking_field(self) -> None:
        assert "thinking" not in _kwargs("anthropic/claude-opus-5", None)


class TestOllamaAnthropicMessagesThinkingOff:
    """Ollama Anthropic-compat: thinking defaults ON for many models when the field is omitted.

    Ollama maps ``thinking: {type: \"disabled\"}`` → ``Think=false`` (docs + source). Hermes must
    send that for ``reasoning_effort: none``/off; ``minimal`` must not force thinking ON.
    """

    OLLAMA_BASES = (
        "http://127.0.0.1:11434",
        "http://localhost:11434/v1",
        "http://ollama:11434",
    )
    NON_CLAUDE = ("gemma4", "qwen3:8b", "llama3.2")

    @pytest.mark.parametrize("base_url", OLLAMA_BASES)
    @pytest.mark.parametrize("model", NON_CLAUDE)
    def test_off_sends_explicit_disabled(self, base_url: str, model: str) -> None:
        kwargs = _kwargs(model, {"enabled": False}, base_url=base_url)
        assert kwargs["thinking"] == {"type": "disabled"}
        assert "output_config" not in kwargs

    @pytest.mark.parametrize("base_url", OLLAMA_BASES)
    @pytest.mark.parametrize("model", NON_CLAUDE)
    def test_minimal_does_not_enable_thinking(self, base_url: str, model: str) -> None:
        """THINKING_BUDGET has no 'minimal' — old path defaulted to budget 8000 = ON."""
        kwargs = _kwargs(model, {"enabled": True, "effort": "minimal"}, base_url=base_url)
        assert kwargs["thinking"] == {"type": "disabled"}
        assert kwargs["thinking"].get("type") != "enabled"

    @pytest.mark.parametrize("base_url", OLLAMA_BASES)
    def test_medium_still_enables_with_budget(self, base_url: str) -> None:
        kwargs = _kwargs("gemma4", {"enabled": True, "effort": "medium"}, base_url=base_url)
        assert kwargs["thinking"]["type"] == "enabled"
        assert kwargs["thinking"]["budget_tokens"] == 8000

    def test_non_ollama_non_claude_off_still_omits(self) -> None:
        """MiniMax / generic third-party: thinking is opt-in; omit remains off (Kimi has its own test)."""
        kwargs = _kwargs(
            "minimax-m2.7",
            {"enabled": False},
            base_url="https://api.minimax.io/anthropic",
        )
        assert "thinking" not in kwargs

    def test_non_ollama_base_does_not_get_ollama_disable(self) -> None:
        """Port/host must look like Ollama — plain localhost without 11434 is not Ollama."""
        kwargs = _kwargs("gemma4", {"enabled": False}, base_url="http://127.0.0.1:8080")
        assert "thinking" not in kwargs


class TestThinkingOffMismatchIsLoud:
    """When disable cannot be honored, never fail silently — session log + doctor."""

    def test_mandatory_model_off_logs_warning(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="agent.anthropic_adapter"):
            kwargs = _kwargs("anthropic/claude-fable-5", {"enabled": False})
        assert "thinking" not in kwargs
        assert any("cannot send thinking.type=disabled" in r.message for r in caplog.records)

    def test_doctor_flags_mandatory_off_under_anthropic_messages(self) -> None:
        from hermes_cli.doctor_config import collect_reasoning_off_mismatch

        msg = collect_reasoning_off_mismatch(
            api_mode="anthropic_messages",
            model="anthropic/claude-fable-5",
            base_url=None,
            reasoning_effort="none",
        )
        assert msg is not None
        assert "cannot send thinking.type=disabled" in msg

    def test_doctor_silent_for_ollama_off(self) -> None:
        from hermes_cli.doctor_config import collect_reasoning_off_mismatch

        assert (
            collect_reasoning_off_mismatch(
                api_mode="anthropic_messages",
                model="gemma4",
                base_url="http://127.0.0.1:11434",
                reasoning_effort="none",
            )
            is None
        )

    def test_doctor_silent_when_disableable_claude(self) -> None:
        from hermes_cli.doctor_config import collect_reasoning_off_mismatch

        assert (
            collect_reasoning_off_mismatch(
                api_mode="anthropic_messages",
                model="claude-opus-4-6",
                base_url=None,
                reasoning_effort=False,
            )
            is None
        )


class TestDisableVerdictHelper:
    """``_accepts_thinking_disable`` is the single source of the verdict."""


    def test_non_claude_models_are_left_alone(self) -> None:
        from agent.anthropic_adapter import _accepts_thinking_disable

        for model in ("minimax-m2.7", "qwen3-max", "kimi-k2.5"):
            assert _accepts_thinking_disable(model) is False, model

    def test_unknown_claude_releases_default_to_disableable(self) -> None:
        """Mirrors _supports_adaptive_thinking: new Claude gets the modern
        contract without a code change."""
        from agent.anthropic_adapter import _accepts_thinking_disable

        assert _accepts_thinking_disable("anthropic/claude-opus-6") is True
