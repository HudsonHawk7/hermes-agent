"""A token cap at or below the fixed prompt must not compact every turn.

The fixed prompt (system prompt plus tool schemas) is tokens compaction can
never reclaim. A configured threshold_tokens / proactive_prune_tokens at or
below that floor trips on turn 1 and every turn after, reclaims nothing, and
breaks the prompt cache. These tests drive the real preflight path.
"""

from __future__ import annotations

import logging
import re

from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_request_tokens_rough
from agent.turn_context_compaction import CompactionOutcome, _preflight_compression

_CAP = 20_000
_CONTEXT = 200_000
_NOTICE = re.compile(
    r"compression\.threshold_tokens 20,000 is below this agent's fixed prompt "
    r"\([\d,]+ tokens\); compacting at [\d,]+ instead\. Raise it in config\.yaml\."
)


def _fat_prompt():
    """A fixed prompt strictly above the 20,000 cap. Archie measured 20,231."""
    system = "x" * 80_924  # ASCII rough estimate is (len + 3) // 4 == 20,231
    tools = [{
        "type": "function",
        "function": {
            "name": "noop",
            "description": "y" * 64,
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    floor = estimate_request_tokens_rough([], system_prompt=system, tools=tools)
    assert floor > _CAP, f"fixture floor {floor} must sit above the cap"
    return system, tools, floor


def _messages(extra=None):
    rows = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "ok"},
    ]
    if extra:
        rows.extend(extra)
    return rows


def _compressor(context_length=_CONTEXT, cap=_CAP):
    return ContextCompressor(
        model="test-model",
        threshold_percent=0.5,
        protect_first_n=1,
        protect_last_n=1,
        summary_target_ratio=0.20,
        quiet_mode=True,
        base_url="",
        api_key="",
        config_context_length=context_length,
        max_tokens=None,
        threshold_tokens_cap=cap,
    )


class _Agent:
    """Just enough of the turn agent for _preflight_compression."""

    def __init__(self, compressor, system_prompt, tools):
        self.context_compressor = compressor
        self.system_prompt = system_prompt
        self.tools = tools
        self.compression_enabled = True
        self.session_id = "fixed-prompt-floor-test"
        self.model = "test-model"
        self.max_compression_attempts = 1
        self.protect_first_n = 1
        self.protect_last_n = 1
        self.warnings = []
        self.compress_calls = 0

    def _emit_warning(self, message):
        self.warnings.append(str(message))

    def _emit_status(self, message):
        return None

    def _compress_context(self, messages, system_message, **kwargs):
        self.compress_calls += 1
        return messages, system_message


def _turn(agent, system, messages=None):
    rows = messages if messages is not None else _messages()
    out = CompactionOutcome(
        messages=rows,
        active_system_prompt=system,
        conversation_history=None,
        current_turn_user_idx=0,
    )
    _preflight_compression(agent, out, system_message=None, user_message="hi", effective_task_id="")


def test_fixed_prompt_above_cap_does_not_compact_every_turn(caplog):
    """Turn 1 and the next turn must not compact. The notice appears once."""
    system, tools, floor = _fat_prompt()
    compressor = _compressor()
    # The provider already reported the fixed prompt, the way Archie's first
    # API call did. A rough estimate above the cap otherwise defers to that
    # report, and the every-turn loop starts on the turn after.
    compressor.last_real_prompt_tokens = floor
    compressor.last_prompt_tokens = floor
    agent = _Agent(compressor, system, tools)

    with caplog.at_level(logging.WARNING, logger="agent.context_compressor_floor"):
        _turn(agent, system)
        after_first = agent.compress_calls
        _turn(agent, system)
        after_second = agent.compress_calls

    assert after_first == 0, "turn 1 compacted; the cap sits under the fixed prompt"
    assert after_second == 0, "the next turn compacted too"
    assert len(agent.warnings) == 1, agent.warnings
    assert _NOTICE.search(agent.warnings[0]), agent.warnings[0]
    assert any(
        rec.levelno >= logging.WARNING
        and "threshold_tokens" in rec.getMessage()
        and "profile " in rec.getMessage()
        and "20,000" in rec.getMessage()
        for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_compacts_when_history_passes_the_effective_threshold():
    """Headroom above the floor is not a permanent off switch."""
    system, tools, floor = _fat_prompt()
    compressor = _compressor()
    compressor.last_real_prompt_tokens = floor
    compressor.last_prompt_tokens = floor
    agent = _Agent(compressor, system, tools)
    _turn(agent, system)
    before = agent.compress_calls
    # A rough estimate above the trigger waits for the provider. This is that
    # report: history has passed the raised trigger.
    compressor.last_real_prompt_tokens = compressor.threshold_tokens
    compressor.last_prompt_tokens = compressor.threshold_tokens
    huge = {"role": "user", "content": "z" * 400_000}
    _turn(agent, system, _messages([huge]))
    assert agent.compress_calls > before


def test_smaller_window_keeps_the_ratio_path():
    """A smaller window still compacts when its ratio trigger is reached.

    The percentage formula is not replaced. The floor raise must not push the
    trigger past that ratio, or the small window would compact late.
    """
    system, tools, floor = _fat_prompt()
    compressor = _compressor()
    compressor.last_real_prompt_tokens = floor
    compressor.last_prompt_tokens = floor
    agent = _Agent(compressor, system, tools)
    _turn(agent, system)

    fresh = _compressor(context_length=32_000)
    fresh.update_model("small-window", 32_000)
    compressor.update_model("small-window", 32_000)
    ratio = compressor._compute_threshold_tokens(32_000, compressor.threshold_percent, None)
    fresh_ratio = fresh._compute_threshold_tokens(32_000, fresh.threshold_percent, None)
    assert ratio == fresh_ratio
    assert compressor.threshold_tokens <= ratio
    assert compressor.threshold_tokens < 32_000
    assert compressor.should_compress(ratio) is True
    assert compressor.should_compress(compressor.threshold_tokens) is True


def test_prune_trigger_below_the_floor_does_not_stay_there():
    """A prune trigger at or below the fixed prompt must be raised above it."""
    system, tools, floor = _fat_prompt()
    compressor = _compressor()
    compressor.proactive_prune_tokens = _CAP
    compressor.last_real_prompt_tokens = floor
    agent = _Agent(compressor, system, tools)
    _turn(agent, system)
    assert compressor.proactive_prune_tokens > floor
