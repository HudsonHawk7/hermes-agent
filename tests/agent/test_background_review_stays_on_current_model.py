"""A background review must not hold a second local model while the chat switches."""
from types import SimpleNamespace

import pytest

from agent.background_review import (
    _BackgroundReviewRun,
    _resolve_review_runtime,
    _review_model_stale,
    abort_background_review_for_model_switch,
)
from agent.agent_runtime_helpers import switch_model


def _local_agent(model="qwen3.8:27b"):
    return SimpleNamespace(
        model=model,
        provider="ollama-local",
        base_url="http://127.0.0.1:11434/v1",
        request_overrides={},
        max_tokens=None,
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        _current_main_runtime=lambda: {
            "api_key": None,
            "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions",
        },
    )


def test_local_review_stays_on_the_chat_model_not_a_second_one():
    agent = _local_agent()
    task = {"provider": "ollama-local", "model": "solara:gemma4-31b-uncensored"}
    runtime = _resolve_review_runtime(agent, task)
    assert runtime["model"] == "qwen3.8:27b"
    assert runtime["routed"] is False


def test_stale_review_does_not_call_the_old_model():
    agent = _local_agent("solara:gemma4-31b-uncensored")
    assert _review_model_stale(agent, {"model": "qwen3.8:27b"}) is True
    assert _review_model_stale(agent, {"model": "solara:gemma4-31b-uncensored"}) is False


def test_model_switch_cancels_the_in_flight_review_before_any_load(monkeypatch):
    order = []
    agent = _local_agent()
    run = _BackgroundReviewRun()
    run.request_done.set()
    agent._background_review_run = run

    def resolve(*args, **kwargs):
        order.append("resolve")
        raise RuntimeError("stop after order check")

    monkeypatch.setattr("agent.agent_runtime_helpers._resolve_switch_destination", resolve)
    with __import__("pytest").raises(RuntimeError, match="stop after order check"):
        switch_model(agent, "solara:gemma4-31b-uncensored", "ollama-local")
    assert order == ["resolve"]
    assert run.cancel_requested.is_set()


def test_abort_reports_an_in_flight_review(monkeypatch):
    agent = _local_agent()
    run = _BackgroundReviewRun()
    agent._background_review_run = run
    seen = {}

    def cancel(agent, *, message, tool_reason):
        seen["message"] = message
        run.request_done.set()
        run.cancel_requested.set()

    monkeypatch.setattr(
        "agent.background_review.cancel_background_review_for_live_turn", cancel,
    )
    assert abort_background_review_for_model_switch(
        agent, new_model="solara:gemma4-31b-uncensored", old_model=agent.model,
    ) is True
    assert "model switch" in seen["message"]


def test_session_model_gate_refuses_while_the_review_holds():
    from agent.background_review import release_cached_review_before_model_load
    agent = _local_agent()
    run = _BackgroundReviewRun()
    agent._background_review_run = run
    runner = SimpleNamespace(_agent_cache={"local:eval": agent}, _agent_cache_lock=None)
    assert release_cached_review_before_model_load(runner, "local:eval", timeout=0.01) is False
    assert run.cancel_requested.is_set()


def _kept_chat():
    return [
        {"role": "user", "content": "the garden stays"},
        {"role": "assistant", "content": "recent answer"},
    ]


def _attach_chat(agent, messages):
    agent._session_messages = [dict(row) for row in messages]
    agent.session = SimpleNamespace(messages=[dict(row) for row in messages], session_id="sess-keep")
    return agent.session


def test_never_wipe_on_model_switch(monkeypatch):
    """A model switch must not clear the chat. A wipe of session.messages fails this."""
    agent = _local_agent()
    kept = _kept_chat()
    session = _attach_chat(agent, kept)

    def resolve(*args, **kwargs):
        assert agent._session_messages == kept
        assert session.messages == kept
        raise RuntimeError("stop before load")

    monkeypatch.setattr("agent.agent_runtime_helpers._resolve_switch_destination", resolve)
    with pytest.raises(RuntimeError, match="stop before load"):
        switch_model(agent, "solara:gemma4-31b-uncensored", "ollama-local")
    assert agent._session_messages == kept
    assert session.messages == kept


def test_running_review_is_cancelled_on_model_switch(monkeypatch):
    """A review that is still running is cancelled before the next model is resolved."""
    order = []
    agent = _local_agent()
    session = _attach_chat(agent, _kept_chat())
    run = _BackgroundReviewRun()
    review = SimpleNamespace(interrupted=False)

    def interrupt(*args, **kwargs):
        review.interrupted = True
        order.append("interrupt")

    review.interrupt = interrupt
    assert run.begin_request(review)
    agent._background_review_run = run

    def inline_interrupt(review_agent, *, message="", tool_reason=""):
        order.append("cancel")
        review_agent.interrupt(message)
        run.request_done.set()

    monkeypatch.setattr("agent.background_review._interrupt_background_review", inline_interrupt)

    def resolve(*args, **kwargs):
        order.append("resolve")
        raise RuntimeError("stop before load")

    monkeypatch.setattr("agent.agent_runtime_helpers._resolve_switch_destination", resolve)
    with pytest.raises(RuntimeError, match="stop before load"):
        switch_model(agent, "solara:gemma4-31b-uncensored", "ollama-local")
    assert order.index("cancel") < order.index("resolve")
    assert review.interrupted is True
    assert run.cancel_requested.is_set()
    assert session.messages == _kept_chat()
    assert agent._session_messages == _kept_chat()


@pytest.mark.asyncio
async def test_session_model_switch_does_not_load_while_review_holds(monkeypatch):
    """session.model must cancel the running review and not resolve the next model while it holds."""
    monkeypatch.setattr(
        "agent.background_review._BACKGROUND_REVIEW_CANCEL_TIMEOUT_SECONDS", 0.01,
    )
    agent = _local_agent()
    session = _attach_chat(agent, _kept_chat())
    run = _BackgroundReviewRun()
    review = SimpleNamespace(interrupted=False)

    def interrupt(*args, **kwargs):
        review.interrupted = True

    review.interrupt = interrupt
    assert run.begin_request(review)
    agent._background_review_run = run
    loaded = []

    def inline_interrupt(review_agent, *, message="", tool_reason=""):
        review_agent.interrupt(message)

    monkeypatch.setattr("agent.background_review._interrupt_background_review", inline_interrupt)

    def current_route(*args, **kwargs):
        loaded.append("route")
        raise AssertionError("next model resolved while review holds the GPU")

    monkeypatch.setattr("gateway.session_mutation_model._current_route", current_route)
    from gateway.session_mutation_model import prepare_model
    from hermes_state_runtime import RuntimeStoreError

    runner = SimpleNamespace(_agent_cache={"local:eval": agent}, _agent_cache_lock=None)
    authority = SimpleNamespace(runner=runner)
    live = SimpleNamespace(route="local:eval")
    with pytest.raises(RuntimeStoreError, match="review_holding_gpu"):
        await prepare_model(authority, live, {}, SimpleNamespace())
    assert loaded == []
    assert review.interrupted is True
    assert run.cancel_requested.is_set()
    assert session.messages == _kept_chat()
