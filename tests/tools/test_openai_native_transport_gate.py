"""openai-native is an autodetect choice only when the main transport can host it.

Codex login creds make the marker provider look ready, but its search() always
fails unless the active main model is provider openai-codex (the Codex Responses
transport). api_mode codex_responses alone is not that transport: xAI and others
persist the same wire mode. An explicit web.search_backend pin is not gated.
"""

from __future__ import annotations


def _isolate_autodetect(monkeypatch, provider: str, api_mode: str = "codex_responses"):
    from tools import web_tools
    import plugins.web.openai_native.provider as native
    from plugins.web.openai_native.provider import OpenAINativeWebSearchProvider

    monkeypatch.setattr(web_tools, "_has_env", lambda _name: False)
    monkeypatch.setattr(web_tools, "_ddgs_package_importable", lambda: False)
    monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: False)
    monkeypatch.setattr(
        web_tools, "_list_registered_web_providers", lambda: [OpenAINativeWebSearchProvider()]
    )
    monkeypatch.setattr(native, "has_codex_credentials", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"provider": provider, "api_mode": api_mode}, "web": {}},
    )
    return web_tools


def test_non_codex_main_with_codex_creds_does_not_autodetect_openai_native(monkeypatch):
    """Hera's shape: xai-oauth + api_mode codex_responses + a Codex login."""
    web_tools = _isolate_autodetect(monkeypatch, "xai-oauth")
    assert web_tools._autodetect_backend() != "openai-native"


def test_ollama_main_with_codex_creds_does_not_autodetect_openai_native(monkeypatch):
    web_tools = _isolate_autodetect(monkeypatch, "ollama", api_mode="")
    assert web_tools._autodetect_backend() != "openai-native"


def test_codex_main_with_codex_creds_still_autodetects_openai_native(monkeypatch):
    web_tools = _isolate_autodetect(monkeypatch, "openai-codex")
    assert web_tools._autodetect_backend() == "openai-native"


def test_codex_provider_with_unset_api_mode_still_autodetects_openai_native(monkeypatch):
    """The overlay declares codex_responses when the mode field was never written."""
    web_tools = _isolate_autodetect(monkeypatch, "openai-codex", api_mode="")
    assert web_tools._autodetect_backend() == "openai-native"


def test_frozen_session_transport_beats_the_config_file(monkeypatch):
    """A mid-session switch is the active transport, not the file on disk."""
    import hermes_cli.runtime_provider as runtime

    web_tools = _isolate_autodetect(monkeypatch, "openai-codex")
    token = runtime._FROZEN_RUNTIME_CONFIG.set(
        {"model": {"provider": "xai-oauth", "api_mode": "codex_responses"}}
    )
    try:
        assert web_tools._autodetect_backend() != "openai-native"
    finally:
        runtime._FROZEN_RUNTIME_CONFIG.reset(token)


def test_explicit_search_backend_pin_is_not_gated_by_main_transport(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"provider": "xai-oauth", "api_mode": "codex_responses"},
            "web": {"search_backend": "openai-native"},
        },
    )
    assert web_tools._get_search_backend() == "openai-native"
