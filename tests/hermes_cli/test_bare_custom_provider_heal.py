"""Bare ``custom`` must heal to its configured identity on the per-turn path (#144437).

A Desktop session can persist the bare billing class ``custom`` — the value a
custom route resolves TO, not a provider name. Ladder rungs 3-4 match only a
configured identity (or an auto/empty config provider), so nothing matched and
the turn fell through to the OpenRouter default endpoint: a fresh
chat on a NAMED local provider shipped to the cloud.
``canonical_custom_identity`` — the healer every session restore path uses — was
never consulted here.
"""

from __future__ import annotations

import pytest

from hermes_cli import runtime_provider as rp

LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
LOCAL_ENTRY = "ollama-local"
LOCAL_MODEL = "local-model:x"


@pytest.fixture
def named_local_provider(monkeypatch):
    """``model.provider: ollama-local`` plus its ``providers.ollama-local`` entry.

    The model block's own ``default`` is a different id on purpose: only the
    caller's ``target_model`` names the local entry, so the heal is proven to use
    the model it was asked about rather than whatever the block happens to say.
    """
    config = {
        "model": {"default": "gpt-4o", "provider": LOCAL_ENTRY},
        "providers": {
            LOCAL_ENTRY: {
                "base_url": LOCAL_BASE_URL,
                "api_mode": "anthropic_messages",
                "api_key": "sk-test",
                "models": {LOCAL_MODEL: {}},
            }
        },
    }
    monkeypatch.setattr(rp, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: config)
    return config


def test_bare_custom_resolves_the_named_local_provider(named_local_provider):
    """The regression: a fresh chat on a named local provider must not reach OpenRouter."""
    runtime = rp.resolve_runtime_provider(requested="custom", target_model=LOCAL_MODEL)

    assert runtime["base_url"] == LOCAL_BASE_URL
    assert "openrouter.ai" not in str(runtime["base_url"])
    assert runtime["api_mode"] == "anthropic_messages"


def test_bare_custom_heals_from_the_configured_provider_when_the_model_is_unlisted(named_local_provider):
    """Second recovery leg: a model no entry advertises still heals via ``model.provider``."""
    runtime = rp.resolve_runtime_provider(requested="custom", target_model="some-new-tag")

    assert runtime["base_url"] == LOCAL_BASE_URL


def test_bare_custom_heals_to_a_keyless_local_provider(monkeypatch):
    """The production shape: a local server needs no key, so the no-auth placeholder must never
    stop the heal — that is the local chat the bug sent to OpenRouter."""
    config = {
        "model": {"default": "gpt-4o", "provider": LOCAL_ENTRY},
        "providers": {LOCAL_ENTRY: {"base_url": LOCAL_BASE_URL, "models": {LOCAL_MODEL: {}}}},
    }
    monkeypatch.setattr(rp, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: config)

    runtime = rp.resolve_runtime_provider(requested="custom", target_model=LOCAL_MODEL)

    assert runtime["base_url"] == LOCAL_BASE_URL


def test_remote_keyless_entry_is_left_unhealed(monkeypatch):
    """#111741 kept: healing onto a remote entry with no credential of its own would ship the no-auth
    placeholder to a host that rejects it, so the bare request keeps failing loud instead."""
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "CUSTOM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    config = {
        "model": {"default": "cloud-model", "provider": "remote-keyless"},
        "providers": {
            "remote-keyless": {
                "base_url": "https://remote.example.invalid/v1",
                "models": {"cloud-model": {}},
            }
        },
    }
    monkeypatch.setattr(rp, "load_config", lambda *a, **k: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: config)

    with pytest.raises(rp.AuthError, match="provider 'custom'"):
        rp.resolve_runtime_provider(requested="custom", target_model="cloud-model")
