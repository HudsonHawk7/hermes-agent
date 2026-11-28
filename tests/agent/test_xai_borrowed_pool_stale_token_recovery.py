"""Borrowed-pool xAI OAuth: adopt root's newer pair, never burn the shared refresh.

Task t_99a52155 (Hera 63-min run, exit 78): a profile worker holds a stale
in-memory access token while the shared root already has a newer pair. Recovery
must re-read root and retry. xAI refresh tokens are single-use — POSTing a
stale (or even a just-adopted) refresh logs every agent out.

The 10/19 profile stubs (empty ``providers.xai-oauth.tokens`` +
``last_auth_error``) must not shadow root. Healing strips them; load falls
through to root. Token values never appear in asserts beyond fixed fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    CredentialPool,
    PooledCredential,
)
from hermes_cli import auth as A


STALE_A = "stale-access-token"
STALE_R = "shared-refresh-v1"
NEWER_A = "newer-access-token"
NEWER_R = "shared-refresh-v2"
_BASE = "https://api.x.ai/v1"


def _write(path: Path, store: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _stub_block() -> dict:
    """10/19-style empty provider block that used to shadow root."""
    return {
        "tokens": {},
        "last_auth_error": {
            "provider": "xai-oauth",
            "code": "xai_refresh_failed",
            "reason": "runtime_refresh_failure",
            "relogin_required": True,
            "at": "2026-10-19T04:00:00Z",
        },
    }


def _root_with_newer_providers_and_stale_pool() -> dict:
    """Root rotated via the singleton path; pool row still holds the old pair."""
    return {
        "version": 1,
        "providers": {
            "xai-oauth": {
                "tokens": {
                    "access_token": NEWER_A,
                    "refresh_token": NEWER_R,
                    "token_type": "Bearer",
                },
                "last_refresh": "2026-10-29T16:37:51Z",
                "auth_mode": "oauth_device_code",
            }
        },
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai1",
                    "label": "device_code",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": "device_code",
                    "access_token": STALE_A,
                    "refresh_token": STALE_R,
                    "base_url": _BASE,
                }
            ]
        },
    }


@pytest.fixture
def profile_borrows_root(tmp_path, monkeypatch):
    """Profile with empty xAI stub; distinct global root holds the live grant."""
    profile_path = tmp_path / "profiles" / "cipher" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-root"))
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root_path)
    A._global_auth_store_cache = None
    A._oauth_heal_clean_marks.clear()
    return profile_path, root_path


def _stale_pool_entry() -> PooledCredential:
    return PooledCredential(
        provider="xai-oauth",
        id="xai1",
        label="device_code",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source="device_code",
        access_token=STALE_A,
        refresh_token=STALE_R,
        base_url=_BASE,
    )


def _ban_refresh_post(monkeypatch):
    """Any POST of the single-use refresh token fails the test."""
    posts: list[tuple[str, str]] = []

    def _boom(access_token, refresh_token, **_kwargs):
        posts.append((access_token, refresh_token))
        raise AssertionError(
            "xAI refresh POST must not run when root already holds a newer pair "
            f"(would burn single-use refresh; posts={len(posts)})"
        )

    monkeypatch.setattr(A, "refresh_xai_oauth_pure", _boom)
    return posts


def test_borrowed_pool_adopts_root_newer_pair_without_refresh_post(
    profile_borrows_root, monkeypatch,
):
    """In-memory stale + root providers already rotated → adopt, never POST."""
    profile_path, root_path = profile_borrows_root
    _write(root_path, _root_with_newer_providers_and_stale_pool())
    _write(
        profile_path,
        {
            "version": 1,
            "providers": {"xai-oauth": _stub_block()},
            "credential_pool": {"xai-oauth": []},
        },
    )
    posts = _ban_refresh_post(monkeypatch)

    pool = CredentialPool("xai-oauth", [_stale_pool_entry()])
    pool._current_id = "xai1"

    recovered = pool.try_refresh_matching(api_key_hint=STALE_A)

    assert recovered is not None
    assert recovered.access_token == NEWER_A
    assert recovered.refresh_token == NEWER_R
    assert posts == [], f"refresh POST burned the shared token: {posts!r}"
    # Root pair must stay intact (no write-back of a posted rotation).
    root = _read(root_path)
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == NEWER_R
    assert root["providers"]["xai-oauth"]["tokens"]["access_token"] == NEWER_A


def test_empty_xai_stub_does_not_shadow_root_provider_state(profile_borrows_root):
    """Profile empty tokens + last_auth_error must fall through to root."""
    profile_path, root_path = profile_borrows_root
    _write(root_path, _root_with_newer_providers_and_stale_pool())
    _write(
        profile_path,
        {
            "version": 1,
            "providers": {"xai-oauth": _stub_block()},
            "credential_pool": {"xai-oauth": []},
        },
    )

    state, source = A._load_provider_state_with_source(
        A._load_auth_store(), "xai-oauth",
    )

    assert state is not None
    assert (state.get("tokens") or {}).get("access_token") == NEWER_A
    assert (state.get("tokens") or {}).get("refresh_token") == NEWER_R
    assert source is not None
    assert Path(source).resolve() == root_path.resolve()


def test_heal_strips_empty_xai_provider_stubs(profile_borrows_root):
    """load_pool / heal removes 10/19 empty stubs so they stop shadowing root."""
    profile_path, root_path = profile_borrows_root
    _write(
        root_path,
        {
            "version": 1,
            "providers": {
                "xai-oauth": {
                    "tokens": {
                        "access_token": NEWER_A,
                        "refresh_token": NEWER_R,
                    },
                    "last_refresh": "2026-10-29T16:37:51Z",
                }
            },
            "credential_pool": {
                "xai-oauth": [
                    {
                        "id": "xai1",
                        "auth_type": "oauth",
                        "source": "device_code",
                        "access_token": NEWER_A,
                        "refresh_token": NEWER_R,
                    }
                ]
            },
        },
    )
    _write(
        profile_path,
        {
            "version": 1,
            "providers": {"xai-oauth": _stub_block()},
            "credential_pool": {"xai-oauth": []},
        },
    )

    from hermes_cli.auth_oauth_grants import heal_forked_single_use_oauth_grants

    A._oauth_heal_clean_marks.clear()
    summary = heal_forked_single_use_oauth_grants("xai-oauth")

    after = _read(profile_path)
    providers = after.get("providers") or {}
    assert "xai-oauth" not in providers, (
        "empty stub must be stripped; left behind it shadows root forever"
    )
    # Root grant untouched.
    root = _read(root_path)
    assert root["providers"]["xai-oauth"]["tokens"]["refresh_token"] == NEWER_R
    assert isinstance(summary, dict)


def test_oauth_stale_recovery_rereads_root_past_profile_stub(
    profile_borrows_root, monkeypatch,
):
    """Tracked client on a stubbed profile adopts root via resolve, no refresh."""
    profile_path, root_path = profile_borrows_root
    _write(root_path, _root_with_newer_providers_and_stale_pool())
    _write(
        profile_path,
        {
            "version": 1,
            "providers": {"xai-oauth": _stub_block()},
            "credential_pool": {"xai-oauth": []},
        },
    )
    posts = _ban_refresh_post(monkeypatch)

    from agent.oauth_stale_recovery import recover_oauth_stale_credentials
    from agent.turn_retry_state import TurnRetryState

    class _Agent:
        def __init__(self) -> None:
            self.api_mode = "codex_responses"
            self.provider = "xai-oauth"
            self.base_url = _BASE
            self.api_key = STALE_A
            self.model = "grok-4"
            self.log_prefix = ""
            self._oauth_tracks_auth_store = True
            self._oauth_auth_store_token = STALE_A
            self._client_kwargs = {"api_key": STALE_A, "base_url": _BASE}
            self.refresh_calls = 0
            self.adopted: list[str] = []

        def _adopt_openai_credentials(self, api_key, base_url, *, reason):
            self.api_key = api_key
            self.base_url = base_url
            self._client_kwargs["api_key"] = api_key
            self._client_kwargs["base_url"] = base_url
            self.adopted.append(reason)
            return True

        def _try_refresh_codex_client_credentials(self, *, force=True):
            self.refresh_calls += 1
            return False

    class _Err(Exception):
        def __init__(self) -> None:
            super().__init__(
                "PermissionDeniedError HTTP 403 "
                "{code: unauthenticated:bad-credentials, "
                "error: The OAuth2 access token could not be validated.}"
            )
            self.status_code = 403

    agent = _Agent()
    verdict = recover_oauth_stale_credentials(agent, _Err(), 403, TurnRetryState())

    assert verdict == "retry"
    assert agent.api_key == NEWER_A
    assert agent.refresh_calls == 0
    assert posts == []
    assert "oauth_auth_store_reread" in agent.adopted
