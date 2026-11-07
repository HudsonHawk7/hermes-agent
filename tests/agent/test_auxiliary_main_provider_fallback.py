"""provider: main must walk the main agent's fallback_providers.

A compression task set to ``provider: main`` means "whatever the main agent
uses". When that main route is capped (payment, quota, or rate-limit), the
call has to walk ``fallback_providers`` the same way ``auto`` does. On base
``main`` is treated as an explicit provider, so a capped main plus a working
fallback still fails.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent import auxiliary_client as ac
from agent.auxiliary_client import call_llm


@pytest.fixture(autouse=True)
def _fresh_unhealthy_cache():
    ac._reset_aux_unhealthy_cache()
    yield
    ac._reset_aux_unhealthy_cache()


def _status(message: str, code: int) -> Exception:
    exc = Exception(f"Error code: {code} - {{'error': {{'message': '{message}'}}}}")
    exc.status_code = code
    return exc


def _client(base_url: str, *, content: str | None = None, error: Exception | None = None):
    client = MagicMock()
    client.base_url = base_url
    create = MagicMock()
    if error is not None:
        create.side_effect = error
    else:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        resp.choices[0].message.tool_calls = None
        resp.choices[0].finish_reason = "stop"
        resp.usage = None
        create.return_value = resp
    client.chat.completions.create = create
    return client


CHAIN = [
    {"provider": "xai-oauth", "model": "grok-4.7"},
    {"provider": "openrouter", "model": "openai/gpt-4o-mini"},
]


def _resolve(capped, first, second):
    def resolve_provider_client(provider, model=None, **_kwargs):
        prov = (provider or "").strip().lower()
        chosen = (model or "").strip().lower()
        if prov == "xai-oauth" or chosen == "grok-4.7":
            return first, "grok-4.7"
        if prov == "openrouter" or "gpt-4o-mini" in chosen:
            return second, "openai/gpt-4o-mini"
        return capped, "deepseek-v4-flash:cloud"

    return resolve_provider_client


def _run(primary_error: Exception):
    capped = _client("http://127.0.0.1:11434/v1", error=primary_error)
    first = _client("https://api.x.ai/v1", error=_status("Weekly usage limit reached", 429))
    second = _client("https://openrouter.ai/api/v1", content="summary-from-fallback")
    with (
        patch.object(ac, "_resolve_task_provider_model", return_value=("main", "deepseek-v4-flash:cloud", None, None, None)),
        patch.object(ac, "_get_cached_client", return_value=(capped, "deepseek-v4-flash:cloud")),
        patch.object(ac, "resolve_provider_client", side_effect=_resolve(capped, first, second)),
        patch.object(ac, "_read_main_provider", return_value="ollama"),
        patch("hermes_cli.config.load_config_readonly", return_value={"fallback_providers": CHAIN}),
        patch("hermes_cli.fallback_config.get_fallback_chain", return_value=[dict(e) for e in CHAIN]),
        patch.object(ac, "_try_payment_fallback", return_value=(None, None, "")),
        patch.object(ac, "_try_main_agent_model_fallback", return_value=(None, None, "")),
    ):
        result = call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize the capped turn"}],
        )
    return result, first, second


@pytest.mark.parametrize(
    "message,code",
    [
        ("Weekly usage limit reached", 429),
        ("Rate limit exceeded, retry in 20s", 429),
        ("Your credit balance is too low", 402),
    ],
)
def test_main_provider_compression_walks_fallback_chain(message, code):
    """Capped main + a working later fallback: compression on provider main succeeds."""
    result, first, second = _run(_status(message, code))
    assert result.choices[0].message.content == "summary-from-fallback"
    assert first.chat.completions.create.call_count == 1
    assert second.chat.completions.create.call_count == 1


def test_main_provider_connection_error_does_not_walk_fallback_chain():
    """A dropped connection is not a cap. provider main must not guess the chain."""
    capped = _client("http://127.0.0.1:11434/v1", error=ConnectionError("connection reset"))
    first = _client("https://api.x.ai/v1", content="should-not-run")
    second = _client("https://openrouter.ai/api/v1", content="should-not-run")
    with (
        patch.object(ac, "_resolve_task_provider_model", return_value=("main", "deepseek-v4-flash:cloud", None, None, None)),
        patch.object(ac, "_get_cached_client", return_value=(capped, "deepseek-v4-flash:cloud")),
        patch.object(ac, "resolve_provider_client", side_effect=_resolve(capped, first, second)),
        patch.object(ac, "_read_main_provider", return_value="ollama"),
        patch("hermes_cli.config.load_config_readonly", return_value={"fallback_providers": CHAIN}),
        patch("hermes_cli.fallback_config.get_fallback_chain", return_value=[dict(e) for e in CHAIN]),
        patch.object(ac, "_try_payment_fallback", return_value=(None, None, "")),
        patch.object(ac, "_try_main_agent_model_fallback", return_value=(None, None, "")),
        pytest.raises(Exception),
    ):
        call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )
    assert not first.chat.completions.create.called
    assert not second.chat.completions.create.called
