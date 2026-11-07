"""OpenAI native web search — declares the Responses API server-side ``web_search`` built-in.

Config: ``web.search_backend: openai-native`` (or ``web.backend``).
Auth: openai-codex OAuth (``hermes auth add openai-codex``); no API key of its own.

Unlike every other provider here, this one never executes a search itself. Selecting it
tells the Codex Responses transport to declare the provider-executed ``web_search`` tool
(``{"type": "web_search"}``) in place of the client-side ``web_search`` function, so the
model drives search server-side. The transport performs that swap; this class exists so
``web.search_backend`` has a real provider name to point at.

Auth gating lives here rather than in the transport because the transport must stay
reachable for a user who configured this backend but has not signed in yet — they get the
clear "sign in" error from :meth:`search` rather than a silently different backend.

Autodetect must not treat a Codex login as enough. ``is_available`` is true only when
that login exists AND the active main model is provider ``openai-codex`` (the transport
that swaps in the built-in). ``api_mode: codex_responses`` alone is not that transport:
xAI and others persist the same wire mode and cannot host it. An explicit
``web.search_backend`` pin is not gated by this check.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from plugins.web._common import BaseWebSearchProvider, search_fail

_UNSUPPORTED_MSG = (
    "openai-native declares OpenAI's server-side web_search tool; it cannot run as a "
    "client-side search and requires the Codex Responses transport (provider "
    "openai-codex). For client-side search use firecrawl (default) or another backend."
)


def _dget(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else None


def has_codex_credentials() -> bool:
    """Cheap probe: True when openai-codex OAuth tokens are *likely* usable.

    Mirrors ``tools/xai_http.has_xai_credentials`` — deliberately avoids
    ``resolve_codex_runtime_credentials`` (disk locks, OAuth network refresh), because
    this runs on every ``hermes tools`` repaint. Checks, fast-to-slow:
    ``providers.openai-codex.tokens.access_token`` in ``auth.json``, then any
    ``credential_pool.openai-codex`` entry carrying an ``access_token`` (pool-only
    multi-account grants never write the providers singleton). Returns False on any
    exception so a corrupted auth store cannot block other availability scans.
    """
    try:
        from hermes_constants import get_hermes_home

        auth_path = get_hermes_home() / "auth.json"
        if not auth_path.exists():
            return False
        store = json.loads(auth_path.read_text(encoding="utf-8-sig"))
        tokens = _dget(_dget(_dget(store, "providers"), "openai-codex"), "tokens")
        if str(_dget(tokens, "access_token") or "").strip():
            return True
        entries = _dget(_dget(store, "credential_pool"), "openai-codex")
        return isinstance(entries, list) and any(
            isinstance(e, dict) and str(e.get("access_token", "") or "").strip() for e in entries
        )
    except Exception:
        return False


_CODEX_PROVIDER = "openai-codex"
_CODEX_API_MODE = "codex_responses"


def active_main_hosts_builtin() -> bool:
    """True when the active main model is the Codex transport that hosts the built-in.

    That transport is provider ``openai-codex``. Its overlay declares api_mode
    ``codex_responses``, but that mode alone is not the host: xAI and others persist
    the same wire mode. A frozen session config wins over the file, so a mid-session
    switch counts. Cheap config read. Fail closed. Never calls a credential resolver.
    """
    try:
        model = _active_model()
        provider = str(model.get("provider") or "").strip().lower()
        if provider != _CODEX_PROVIDER:
            return False
        api_mode = str(model.get("api_mode") or "").strip().lower()
        return not api_mode or api_mode == _CODEX_API_MODE
    except Exception:
        return False


def _active_model() -> dict:
    """Model block of the active runtime config, or ``{}``. Frozen session first."""
    try:
        import sys

        runtime = sys.modules.get("hermes_cli.runtime_provider")
        if runtime is not None:
            frozen = runtime._FROZEN_RUNTIME_CONFIG.get()
            if isinstance(frozen, dict):
                model = frozen.get("model")
                return model if isinstance(model, dict) else {}
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model = cfg.get("model") if isinstance(cfg, dict) else None
        return model if isinstance(model, dict) else {}
    except Exception:
        return {}


class OpenAINativeWebSearchProvider(BaseWebSearchProvider):
    """Marker provider: the Codex Responses transport swaps the client ``web_search``
    function for the server-executed built-in when this backend is active."""

    NAME = "openai-native"
    DISPLAY_NAME = "OpenAI Native Web Search (Codex Responses)"

    def is_available(self) -> bool:
        # Creds alone are not enough: a pool login on an xAI/ollama main transport
        # makes autodetect pick this backend, then every search fails client-side.
        return has_codex_credentials() and active_main_hosts_builtin()

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Never called on a successful native turn — the transport replaces the tool
        before the request goes out. Reached only when the active transport cannot host
        the built-in, so fail loudly instead of returning an empty result set."""
        return search_fail(_UNSUPPORTED_MSG)

    def get_setup_schema(self) -> dict[str, Any]:
        from plugins.web._common import setup_schema

        return setup_schema(
            self.DISPLAY_NAME,
            "native",
            "Search runs on the provider side (needs the Codex Responses transport + an openai-codex login); search only, extraction still uses another backend",
            "",
        )
