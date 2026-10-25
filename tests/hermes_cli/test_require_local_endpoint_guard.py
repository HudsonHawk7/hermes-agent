"""``model.require_local_endpoint`` — the opt-in egress guard (#144437).

With the key true, a runtime whose resolved base_url is not a local host is
refused with a typed ``AuthError`` naming the setting, instead of shipping the
turn to a cloud API. The error type matters: ``resolve_runtime_with_fallback``
retries on ``AuthError``, so the guard must ALSO hold on every fallback entry —
a fallback may never land on a non-local endpoint while the setting is true.
"""

from __future__ import annotations

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.auth import AuthError

LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
CLOUD_BASE_URL = "https://api.cloud-endpoint.invalid/v1"
LOCAL_MODEL = "local-model:x"
CLOUD_MODEL = "cloud-model"


def _config(*, require_local_endpoint, fallback_providers=()):
    model = {"default": CLOUD_MODEL, "provider": "custom:cloud-endpoint"}
    if require_local_endpoint is not None:
        model["require_local_endpoint"] = require_local_endpoint
    return {
        "model": model,
        "providers": {
            "cloud-endpoint": {"base_url": CLOUD_BASE_URL, "api_key": "sk-cloud", "models": {CLOUD_MODEL: {}}},
            "local-box": {
                "base_url": LOCAL_BASE_URL,
                "api_mode": "anthropic_messages",
                "api_key": "sk-local",
                "models": {LOCAL_MODEL: {}},
            },
        },
        "fallback_providers": list(fallback_providers),
    }


@pytest.fixture
def config_for(monkeypatch):
    def _apply(config):
        monkeypatch.setattr(rp, "load_config", lambda *a, **k: config)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: config)
        return config

    return _apply


def test_non_local_endpoint_is_refused_when_required(config_for):
    config_for(_config(require_local_endpoint=True))

    with pytest.raises(AuthError, match="model.require_local_endpoint"):
        rp.resolve_runtime_provider(requested="custom:cloud-endpoint", target_model=CLOUD_MODEL)


def test_local_endpoint_passes_when_required(config_for):
    config_for(_config(require_local_endpoint=True))

    runtime = rp.resolve_runtime_provider(requested="custom:local-box", target_model=LOCAL_MODEL)

    assert runtime["base_url"] == LOCAL_BASE_URL


def test_guard_is_off_unless_the_setting_is_true(config_for):
    """No auto-default: an absent key keeps today's resolution untouched."""
    config_for(_config(require_local_endpoint=None))

    runtime = rp.resolve_runtime_provider(requested="custom:cloud-endpoint", target_model=CLOUD_MODEL)

    assert runtime["base_url"] == CLOUD_BASE_URL


def test_fallback_walk_never_lands_on_a_non_local_endpoint(config_for):
    """The guard holds on the fallback walk: a cloud entry is refused exactly like the primary,
    so the walk continues and lands on the configured local entry."""
    config = config_for(
        _config(
            require_local_endpoint=True,
            fallback_providers=[
                {"provider": "custom:cloud-endpoint", "model": CLOUD_MODEL},
                {"provider": "custom:local-box", "model": LOCAL_MODEL},
            ],
        )
    )

    runtime, entry = rp.resolve_runtime_with_fallback(
        config, requested="custom:cloud-endpoint", target_model=CLOUD_MODEL
    )

    assert runtime["base_url"] == LOCAL_BASE_URL
    assert "openrouter.ai" not in str(runtime["base_url"])
    assert entry is not None and entry["provider"] == "custom:local-box"


def test_fallback_walk_raises_rather_than_ship_to_the_cloud(config_for):
    """No local entry in the chain: the primary guard error is re-raised, never a cloud runtime."""
    config = config_for(
        _config(
            require_local_endpoint=True,
            fallback_providers=[{"provider": "openrouter", "model": "some-cloud-model"}],
        )
    )

    with pytest.raises(AuthError, match="model.require_local_endpoint"):
        rp.resolve_runtime_with_fallback(config, requested="custom:cloud-endpoint", target_model=CLOUD_MODEL)
