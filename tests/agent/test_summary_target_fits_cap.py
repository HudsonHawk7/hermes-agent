"""The summary prompt must ask for less than the wire cap, or startup refuses the cap."""
import re

import pytest

from agent.agent_init import _parse_compression_config
from agent.context_compressor import (
    ContextCompressor,
    minimum_summary_wire_cap,
    summary_cap_too_small,
)


def test_cap_below_the_floor_is_a_loud_config_error():
    agent = type("A", (), {"model": "eval-model", "provider": "ollama-local", "api_mode": "chat_completions"})()
    with pytest.raises(ValueError, match="compression.summary_max_tokens"):
        _parse_compression_config(agent, {"compression": {"summary_max_tokens": 800}})
    assert summary_cap_too_small(800)
    assert minimum_summary_wire_cap() > 2500


def test_legal_cap_is_accepted():
    agent = type("A", (), {"model": "eval-model", "provider": "ollama-local", "api_mode": "chat_completions"})()
    settings = _parse_compression_config(
        agent, {"compression": {"summary_max_tokens": minimum_summary_wire_cap()}},
    )
    assert settings.summary_max_tokens == minimum_summary_wire_cap()


def test_compress_cap_800_asks_for_at_most_600_and_a_fitting_summary_succeeds(monkeypatch):
    """Revert fit_summary_prompt_target and this fails: today's prompt asks for ~6000."""
    seen = {}

    def fake_summary(self, prompt, _started):
        seen["prompt"] = prompt
        return "Kept the recent garden talk. Dropped the old picture prompt."

    monkeypatch.setattr(ContextCompressor, "_call_summary_llm", fake_summary)
    compressor = ContextCompressor(
        model="eval-model",
        protect_first_n=0,
        protect_last_n=2,
        summary_max_tokens=800,
        tail_mode="lean",
        quiet_mode=True,
    )
    compressor.tail_token_budget = 40
    messages = [{"role": "system", "content": "You are the eval companion."}]
    for i in range(16):
        messages.append({"role": "user", "content": f"middle turn {i} " + ("x" * 80)})
        messages.append({"role": "assistant", "content": f"middle reply {i}"})
    messages.append({"role": "user", "content": "recent question"})
    messages.append({"role": "assistant", "content": "recent answer"})

    out = compressor.compress(messages, force=True)
    prompt = seen.get("prompt") or ""
    match = re.search(r"Target ~(\d+)", prompt)
    assert match, prompt[:400]
    assert int(match.group(1)) <= 600
    assert any("garden talk" in str(m.get("content") or "") for m in out)
