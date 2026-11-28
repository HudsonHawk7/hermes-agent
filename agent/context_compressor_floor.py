"""Raise a compression cap that sits at or below the fixed prompt.

threshold_tokens and proactive_prune_tokens are absolute counts. They survive
model switches. When a cap is at or below the tokens compaction can never
reclaim (system prompt plus tool schemas) plus the post-compaction tail, every
turn trips the trigger, reclaims nothing, and breaks the prompt cache. This
module raises that session effective trigger above the floor and says so once.
A smaller window whose ratio already sits at or below the floor keeps the ratio.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# (session_id, notice token) so a gateway rebuild of the same session does not
# warn again. The compressor instance keeps its own set for sessions with no id.
_SESSION_NOTICES = set()
_MEASURE_FAILED = False


def measure_fixed_prompt_tokens(system_prompt, tools):
    """Tokens compaction can never reclaim: system prompt plus tool schemas."""
    global _MEASURE_FAILED
    try:
        from agent.model_metadata import estimate_request_tokens_rough
        return int(estimate_request_tokens_rough(
            [], system_prompt=system_prompt or "", tools=tools or None,
        ))
    except Exception:
        if not _MEASURE_FAILED:
            _MEASURE_FAILED = True
            logger.warning("fixed prompt floor measure failed", exc_info=True)
        return 0


def tail_target_tokens(compressor, context_length):
    """Post-compaction tail the compressor will keep for this window.

    Matches tail_token_budget in lean mode without reading threshold_tokens
    (that property calls back into the cap path).
    """
    from agent.context_compressor import (
        LEAN_TAIL_CAP_TOKENS,
        LEAN_TAIL_FLOOR_TOKENS,
        TAIL_MAX_CONTEXT_FRACTION,
    )
    length = int(context_length or 0)
    cached = getattr(compressor, "_tail_token_budget", None)
    same_window = int(getattr(compressor, "context_length", 0) or 0) == length
    if same_window and isinstance(cached, int) and not isinstance(cached, bool) and cached > 0:
        return cached
    if getattr(compressor, "tail_mode", "lean") == "lean":
        budget = max(LEAN_TAIL_FLOOR_TOKENS, min(LEAN_TAIL_CAP_TOKENS, int(length * 0.025)))
    else:
        ratio = float(getattr(compressor, "summary_target_ratio", 0.2) or 0.2)
        budget = int(length * ratio)
    if length > 0:
        budget = min(budget, int(length * TAIL_MAX_CONTEXT_FRACTION))
    return max(1, budget)


def cap_trigger_tokens(compressor, ratio_tokens, context_length, *, honor_aux):
    """Configured cap, then the aux window, then a raise above the fixed floor.

    honor_aux is False for the preview path, which excludes the auxiliary ceiling.
    """
    cap = compressor._effective_threshold_cap(context_length)
    tokens = min(int(ratio_tokens), cap) if cap is not None else int(ratio_tokens)
    aux = getattr(compressor, "_aux_context_ceiling", None) if honor_aux else None
    if isinstance(aux, int) and not isinstance(aux, bool) and 0 < aux < tokens:
        tokens = aux
    floor = int(getattr(compressor, "_fixed_prompt_floor", 0) or 0)
    if floor <= 0 or cap is None:
        return tokens
    headroom = floor + tail_target_tokens(compressor, context_length)
    if cap > headroom or int(ratio_tokens) <= headroom:
        return tokens
    raised = headroom + 1
    if isinstance(aux, int) and not isinstance(aux, bool) and 0 < aux < raised:
        return tokens
    used = min(raised, int(ratio_tokens))
    configured = int(getattr(compressor, "threshold_tokens_cap", 0) or 0)
    if used > configured > 0:
        _queue_notice(
            compressor, key="threshold_tokens", configured=configured,
            floor=floor, used=used, verb="compacting",
        )
    return used


def apply_threshold_cap(compressor):
    """Clamp the live trigger: configured cap, aux window, fixed-prompt floor."""
    ctx = compressor.context_length
    ratio = compressor._compute_threshold_tokens(ctx, compressor.threshold_percent, compressor.max_tokens)
    guarded = cap_trigger_tokens(compressor, ratio, ctx, honor_aux=True)
    if guarded != compressor._threshold_tokens:
        compressor.threshold_tokens = guarded


def note_fixed_prompt_floor(agent, compressor, *, system_prompt):
    """Measure the floor, raise a useless cap, and say so once for this session."""
    if compressor is None:
        return
    floor = measure_fixed_prompt_tokens(system_prompt, getattr(agent, "tools", None))
    if floor <= 0:
        return
    compressor._floor_notice_profile = _profile_name()
    compressor._floor_notice_session = str(getattr(agent, "session_id", "") or "")
    if int(getattr(compressor, "_fixed_prompt_floor", 0) or 0) != floor:
        compressor._fixed_prompt_floor = floor
        compressor._threshold_tokens = None
    # Install the raised trigger before should_compress or the cheap gate reads it.
    _ = compressor.threshold_tokens
    _guard_prune(compressor, floor)
    _emit(agent, _drain(compressor))


def _guard_prune(compressor, floor):
    configured = int(getattr(compressor, "proactive_prune_tokens", 0) or 0)
    if configured <= 0 or configured > floor:
        return
    used = floor + 1
    compressor.proactive_prune_tokens = used
    _queue_notice(
        compressor, key="proactive_prune_tokens", configured=configured,
        floor=floor, used=used, verb="pruning",
    )


def _profile_name():
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name()
    except Exception:
        logger.warning("could not read the active profile name", exc_info=True)
        name = ""
    return str(name or "unknown")


def _chat_text(key, configured, floor, used, verb):
    return (
        f"compression.{key} {configured:,} is below this agent's fixed prompt "
        f"({floor:,} tokens); {verb} at {used:,} instead. Raise it in config.yaml."
    )


def _queue_notice(compressor, *, key, configured, floor, used, verb):
    token = (key, configured, floor, used)
    shown = getattr(compressor, "_floor_notices_shown", None)
    if shown is None:
        compressor._floor_notices_shown = shown = set()
    session_id = str(getattr(compressor, "_floor_notice_session", "") or "")
    if token in shown or (session_id and (session_id, token) in _SESSION_NOTICES):
        shown.add(token)
        return
    shown.add(token)
    if session_id:
        _SESSION_NOTICES.add((session_id, token))
    profile = getattr(compressor, "_floor_notice_profile", None) or "unknown"
    text = _chat_text(key, configured, floor, used, verb)
    logger.warning("profile %s: %s", profile, text)
    pending = getattr(compressor, "_pending_floor_notices", None)
    if pending is None:
        compressor._pending_floor_notices = pending = []
    pending.append(text)


def _drain(compressor):
    pending = getattr(compressor, "_pending_floor_notices", None) or []
    compressor._pending_floor_notices = []
    return list(pending)


def _emit(agent, notices):
    if not notices:
        return
    text = notices[0] if len(notices) == 1 else "\n".join(notices)
    try:
        agent._compression_warning = text
    except Exception:
        logger.warning("could not store the fixed-prompt floor notice", exc_info=True)
    for name in ("_emit_warning", "_emit_diagnostic_status", "_safe_print"):
        fn = getattr(agent, name, None)
        if callable(fn):
            fn(text)
            return
    logger.warning("fixed-prompt floor notice had no chat sink: %s", text)
