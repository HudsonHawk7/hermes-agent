"""One-shot recovery when a long-running OAuth client holds a stale access token.

A sibling process can rotate the auth-store token while this process keeps the
old one. The next 401, or a 403 ``bad-credentials``, used to be terminal
(exit 78) even though a fresh process authenticated with the store's token.

Only a client that loaded its key from the auth store may adopt a newer store
token. A manual key or a pool entry must not be replaced by the singleton
(the account-swap guard in ``_try_refresh_codex_client_credentials``).

Order, once per retry state, never a loop:
1. re-read the store and retry if it holds a different token;
2. otherwise refresh once and retry;
3. then fail.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TRACKED_PROVIDERS = frozenset({"openai-codex", "xai-oauth"})
_STALE_403_MARKERS = (
    "bad-credentials",
    "could not be validated",
    "invalid_api_key",
    "token_expired",
)


def note_oauth_auth_store(agent: Any, runtime: Any) -> None:
    """Remember that this client loaded its key from the profile auth store.

    Call this right after constructing an agent from ``resolve_runtime_provider``.
    A later 401/403 may then adopt a sibling's rotated token. A runtime whose
    source is not the auth store is left unmarked, so a manual key is never
    swapped for the singleton.
    """
    if not isinstance(runtime, dict) or runtime.get("source") != "hermes-auth-store":
        return
    provider = runtime.get("provider") or getattr(agent, "provider", "") or ""
    if provider not in _TRACKED_PROVIDERS:
        return
    key = str(runtime.get("api_key") or getattr(agent, "api_key", "") or "").strip()
    if not key:
        return
    agent._oauth_tracks_auth_store = True
    agent._oauth_auth_store_token = key


def note_oauth_loaded_from_store(agent: Any) -> None:
    """Mark the client when the key it holds is the auth store's current token.

    Covers constructors that spread runtime kwargs and never see ``source``.
    A key that does not match the store (manual, pool, or already rotated) is
    left unmarked. Never raises: a missing store must not break agent init.
    """
    if getattr(agent, "_oauth_tracks_auth_store", False):
        return
    if (getattr(agent, "provider", "") or "") not in _TRACKED_PROVIDERS:
        return
    held = str(getattr(agent, "api_key", "") or "").strip()
    if not held:
        return
    store_key, _base = _store_credentials(agent)
    if store_key and store_key == held:
        agent._oauth_tracks_auth_store = True
        agent._oauth_auth_store_token = held


def recover_oauth_stale_credentials(agent: Any, api_error: Exception, status_code: Any, retry: Any) -> str:
    """``retry`` / ``terminal`` / ``skip``.

    ``skip`` means this error is not ours; the caller keeps its existing path.
    ``terminal`` means we already reread and refreshed once; do not refresh again.
    """
    if not _is_stale_oauth_rejection(agent, api_error, status_code):
        return "skip"
    if not getattr(agent, "_oauth_tracks_auth_store", False):
        return "skip"
    held = str(getattr(agent, "api_key", "") or "").strip()
    remembered = str(getattr(agent, "_oauth_auth_store_token", "") or "").strip()
    # A pool swap replaced the key we loaded. That entry is the pool's, not the singleton's.
    if held and remembered and held != remembered:
        return "skip"

    if not getattr(retry, "oauth_stale_reread_attempted", False):
        retry.oauth_stale_reread_attempted = True
        store_key, store_base = _store_credentials(agent)
        if store_key and store_key != held:
            base = store_base or str(getattr(agent, "base_url", "") or "").strip()
            try:
                adopted = bool(agent._adopt_openai_credentials(
                    store_key, base, reason="oauth_auth_store_reread",
                ))
            except Exception:
                logger.debug("oauth auth-store adopt failed", exc_info=True)
                adopted = False
            if adopted:
                agent._oauth_auth_store_token = store_key
                _say(agent, "reread")
                return "retry"

    if not getattr(retry, "oauth_stale_refresh_attempted", False):
        retry.oauth_stale_refresh_attempted = True
        try:
            refreshed = bool(agent._try_refresh_codex_client_credentials(force=True))
        except Exception:
            logger.debug("oauth provider refresh failed", exc_info=True)
            refreshed = False
        if refreshed:
            agent._oauth_auth_store_token = str(getattr(agent, "api_key", "") or "").strip()
            _say(agent, "refresh")
            return "retry"
        _say_terminal(agent)
        return "terminal"

    _say_terminal(agent)
    return "terminal"


def _is_stale_oauth_rejection(agent: Any, api_error: Exception, status_code: Any) -> bool:
    if (getattr(agent, "provider", "") or "") not in _TRACKED_PROVIDERS:
        return False
    if status_code == 401:
        return True
    if status_code != 403:
        return False
    blob = str(api_error).lower()
    return any(marker in blob for marker in _STALE_403_MARKERS)


def _store_credentials(agent: Any) -> tuple[str, str]:
    provider = getattr(agent, "provider", "") or ""
    try:
        from hermes_cli import auth as _auth
        resolve = (
            _auth.resolve_codex_runtime_credentials if provider == "openai-codex"
            else _auth.resolve_xai_oauth_runtime_credentials
        )
        creds = resolve(refresh_if_expiring=False) or {}
    except Exception:
        logger.debug("oauth auth-store reread failed", exc_info=True)
        return "", ""
    if not isinstance(creds, dict):
        return "", ""
    return str(creds.get("api_key") or "").strip(), str(creds.get("base_url") or "").strip()


def _say(agent: Any, step: str) -> None:
    provider = getattr(agent, "provider", "") or ""
    if step == "reread":
        logger.info("OAuth bad-credentials recovered by re-reading the auth store (%s)", provider)
        line = "🔐 OAuth access token was stale — adopted the newer token from the auth store and retrying..."
    else:
        logger.info("OAuth bad-credentials recovered by provider refresh (%s)", provider)
        line = "🔐 OAuth access token was rejected — refreshed credentials and retrying..."
    try:
        from agent.turn_recovery import _vlines
        _vlines(agent, line)
    except Exception:
        logger.debug("oauth recovery notice skipped", exc_info=True)


def _say_terminal(agent: Any) -> None:
    logger.info(
        "OAuth bad-credentials still rejected after auth-store reread and one provider refresh (%s)",
        getattr(agent, "provider", "") or "",
    )
