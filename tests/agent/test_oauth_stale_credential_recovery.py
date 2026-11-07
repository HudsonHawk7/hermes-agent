"""A long-running OAuth client must not die on a stale access token.

The 11/06 failure: API call 170 succeeded, call 171 got HTTP 403
``unauthenticated:bad-credentials``, and the worker exited 78. The auth store
already held a newer token (a sibling had refreshed it). A fresh process
authenticated. The long-running process never re-read the store.

Contract:
- a tracked client (it loaded this token from the auth store) re-reads the
  store and retries once with the newer token, without calling refresh;
- if the store still has the rejected token, it refreshes once and retries;
- a second rejection on the same retry state is terminal, and refresh is not
  called again;
- a client that did not load from the store does not adopt the singleton.
"""

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_recovery import recover_after_classification
from agent.turn_retry_state import TurnRetryState

_STALE = "stale-access-token"
_NEWER = "newer-access-token"
_REFRESHED = "refreshed-access-token"
_BASE = "https://api.x.ai/v1"


class _PermissionDenied(Exception):
    def __init__(self, status_code: int = 403) -> None:
        super().__init__(
            "PermissionDeniedError ... HTTP 403 {code: unauthenticated:bad-credentials, "
            "error: The OAuth2 access token could not be validated.}"
        )
        self.status_code = status_code
        self.body = {
            "error": {
                "code": "unauthenticated:bad-credentials",
                "message": "The OAuth2 access token could not be validated.",
            }
        }


class _Agent:
    def __init__(self, *, tracks: bool = True) -> None:
        self.api_mode = "codex_responses"
        self.provider = "xai-oauth"
        self.base_url = _BASE
        self.api_key = _STALE
        self.model = "grok-4"
        self.log_prefix = ""
        self._oauth_tracks_auth_store = tracks
        self._oauth_auth_store_token = _STALE if tracks else ""
        self._client_kwargs = {"api_key": _STALE, "base_url": _BASE}
        self.refresh_calls = 0
        self.refresh_result = False
        self.adopted = []

    def _get_transport(self):
        return None

    def _extract_api_error_context(self, error):
        return {}

    def _recover_with_credential_pool(self, **kwargs):
        return False, False

    def _adopt_openai_credentials(self, api_key, base_url, *, reason):
        self.api_key = api_key
        self.base_url = base_url
        self._client_kwargs["api_key"] = api_key
        self._client_kwargs["base_url"] = base_url
        self.adopted.append(reason)
        return True

    def _try_refresh_codex_client_credentials(self, *, force=True):
        self.refresh_calls += 1
        if not self.refresh_result:
            return False
        self.api_key = _REFRESHED
        self._client_kwargs["api_key"] = _REFRESHED
        return True


def _store(api_key: str):
    def _resolve(**_kwargs):
        return {"api_key": api_key, "base_url": _BASE, "source": "hermes-auth-store"}

    return _resolve


def _recover(agent, retry, status_code=403):
    err = _PermissionDenied(status_code)
    classified = classify_api_error(err, provider="xai-oauth", base_url=_BASE)
    return recover_after_classification(
        agent, err, classified, retry,
        status_code=status_code, error_context={}, messages=[], api_messages=[],
    )


def test_bad_credentials_403_is_not_retryable():
    classified = classify_api_error(_PermissionDenied(), provider="xai-oauth", base_url=_BASE)
    assert classified.reason is FailoverReason.auth
    assert classified.retryable is False
    assert classified.status_code == 403


def test_stale_token_reread_retries_with_the_store_token(monkeypatch):
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", _store(_NEWER))
    agent = _Agent()
    retry = TurnRetryState()

    ok, _pool = _recover(agent, retry)

    assert ok is True
    assert agent.api_key == _NEWER
    assert agent._client_kwargs["api_key"] == _NEWER
    assert agent.refresh_calls == 0
    assert "oauth_auth_store_reread" in agent.adopted


def test_tracked_401_also_rereads_before_refresh(monkeypatch):
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", _store(_NEWER))
    agent = _Agent()

    ok, _pool = _recover(agent, TurnRetryState(), status_code=401)

    assert ok is True
    assert agent.api_key == _NEWER
    assert agent.refresh_calls == 0


def test_revoked_token_fails_after_one_refresh(monkeypatch):
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", _store(_STALE))
    agent = _Agent()
    agent.refresh_result = False
    retry = TurnRetryState()

    first, _pool = _recover(agent, retry)
    second, _pool = _recover(agent, retry)

    assert first is False
    assert second is False
    assert agent.refresh_calls == 1
    assert agent.api_key == _STALE


def test_refresh_retries_once_then_a_second_rejection_is_terminal(monkeypatch):
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", _store(_STALE))
    agent = _Agent()
    agent.refresh_result = True
    retry = TurnRetryState()

    first, _pool = _recover(agent, retry)
    second, _pool = _recover(agent, retry)

    assert first is True
    assert agent.api_key == _REFRESHED
    assert second is False
    assert agent.refresh_calls == 1


def test_untracked_client_does_not_adopt_the_store_token(monkeypatch):
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", _store(_NEWER))
    agent = _Agent(tracks=False)

    ok, _pool = _recover(agent, TurnRetryState())

    assert ok is False
    assert agent.api_key == _STALE
    assert agent.refresh_calls == 0
    assert agent.adopted == []


def test_note_tracks_only_a_key_loaded_from_the_store(monkeypatch):
    from agent.oauth_stale_recovery import note_oauth_auth_store, note_oauth_loaded_from_store

    matching = _Agent(tracks=False)
    matching.api_key = _STALE
    monkeypatch.setattr("hermes_cli.auth.resolve_xai_oauth_runtime_credentials", _store(_STALE))
    note_oauth_loaded_from_store(matching)
    assert matching._oauth_tracks_auth_store is True
    assert matching._oauth_auth_store_token == _STALE

    manual = _Agent(tracks=False)
    manual.api_key = "manual-key"
    note_oauth_loaded_from_store(manual)
    assert manual._oauth_tracks_auth_store is False

    stamped = _Agent(tracks=False)
    note_oauth_auth_store(stamped, {"source": "hermes-auth-store", "api_key": _NEWER})
    assert stamped._oauth_tracks_auth_store is True
    assert stamped._oauth_auth_store_token == _NEWER

    other = _Agent(tracks=False)
    note_oauth_auth_store(other, {"source": "env", "api_key": "manual-key"})
    assert other._oauth_tracks_auth_store is False
