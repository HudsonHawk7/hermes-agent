"""A background review must not hold a second local model while the chat switches."""
from types import SimpleNamespace

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
