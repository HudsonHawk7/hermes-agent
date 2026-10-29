"""Device-code sign-in + revoke for Work watch (public client, no secret).

Refresh token lives in the profile ``.env`` as ``GRAPH_WATCH_REFRESH_TOKEN`` via
``save_env_value`` / ``remove_env_value``. Access tokens stay in-process memory only.

Device-code and refresh use httpx (already a Hermes dependency). msal is never
required — AC-P1 / Probe defect 1.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from plugins.work_watch.constants import (
    AAD_ADMIN_REQUIRED_94,
    AAD_ADMIN_REQUIRED_95,
    AAD_CONSENT_MISSING,
    AAD_USER_DECLINED,
    CLIENT_ID_ENV,
    DEFAULT_AUTHORITY_TENANT,
    DEVICE_LOGIN_URL,
    REFRESH_TOKEN_ENV,
    SCOPES,
    STATUS_NEEDS_SIGN_IN,
    STATUS_OFF,
    STATUS_SIGN_IN_IN_PROGRESS,
    STATUS_SIGNED_IN,
    TENANT_ENV,
)

logger = logging.getLogger(__name__)

# RFC 8628 device-code grant type (Microsoft identity platform v2).
_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# In-memory only — never written to disk/logs/cards.
_access_token: Optional[str] = None
_access_expires_at: float = 0.0
_access_lock = threading.Lock()

# Active device-code session (one at a time). RLock so finish/cancel helpers can nest.
_device_lock = threading.RLock()
_device_session: Optional[DeviceCodeSession] = None
_poller_stop = threading.Event()
_poller_thread: Optional[threading.Thread] = None

# Injectable httpx client factory for tests (returns a client used as context manager).
_httpx_client_factory: Optional[Callable[[], Any]] = None


@dataclass
class DeviceCodeSession:
    """In-flight device-code sign-in (memory only)."""

    user_code: str
    verification_uri: str
    message: str
    expires_at: float
    flow: dict[str, Any]
    started_at: float = field(default_factory=time.time)
    status: str = STATUS_SIGN_IN_IN_PROGRESS
    error_code: Optional[str] = None
    error_plain: Optional[str] = None
    done: bool = False
    cancelled: bool = False


@dataclass
class AuthOutcome:
    """Result of a sign-in attempt or status probe (never carries tokens)."""

    ok: bool
    status: str
    error_code: Optional[str] = None
    error_plain: Optional[str] = None
    identity_masked: Optional[str] = None
    has_refresh_token: bool = False


def set_httpx_client_factory(factory: Optional[Callable[[], Any]]) -> None:
    """Test seam: inject a fake httpx.Client factory."""
    global _httpx_client_factory
    _httpx_client_factory = factory


def _http_client() -> Any:
    if _httpx_client_factory is not None:
        return _httpx_client_factory()
    return httpx.Client(timeout=30.0)


def _getenv(name: str, default: str = "") -> str:
    """Read env; prefer secret_scope so profile .env is respected."""
    try:
        from agent.secret_scope import get_secret

        val = get_secret(name)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    except Exception:
        pass
    import os

    return (os.environ.get(name) or default).strip()


def has_refresh_token() -> bool:
    """True when a non-empty refresh token is configured (NULL/absent = off)."""
    raw = _getenv(REFRESH_TOKEN_ENV, "")
    if not raw or raw.upper() == "NULL":
        return False
    return True


def get_client_id() -> str:
    return _getenv(CLIENT_ID_ENV, "")


def get_tenant() -> str:
    return _getenv(TENANT_ENV, DEFAULT_AUTHORITY_TENANT) or DEFAULT_AUTHORITY_TENANT


def _authority_base(tenant: Optional[str] = None) -> str:
    return f"https://login.microsoftonline.com/{tenant or get_tenant()}"


def _device_code_endpoint(tenant: Optional[str] = None) -> str:
    return f"{_authority_base(tenant)}/oauth2/v2.0/devicecode"


def _token_endpoint(tenant: Optional[str] = None) -> str:
    return f"{_authority_base(tenant)}/oauth2/v2.0/token"


def mask_identity(email_or_upn: Optional[str]) -> Optional[str]:
    """Mask identity for UI/receipts (e.g. j***@contoso.com). Never logs raw."""
    if not email_or_upn or "@" not in email_or_upn:
        return None
    local, _, domain = email_or_upn.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _save_refresh_token(token: str) -> None:
    """Persist refresh token only — never access token."""
    from hermes_cli.config import save_env_value

    if not token or not str(token).strip():
        raise ValueError("refuse empty refresh token")
    save_env_value(REFRESH_TOKEN_ENV, str(token).strip())


def _clear_refresh_token() -> bool:
    from hermes_cli.config import remove_env_value

    return bool(remove_env_value(REFRESH_TOKEN_ENV))


def _clear_access_token() -> None:
    global _access_token, _access_expires_at
    with _access_lock:
        _access_token = None
        _access_expires_at = 0.0


def set_access_token_memory(token: Optional[str], expires_in: float = 3600.0) -> None:
    """Store access token in process memory only (tests + successful refresh)."""
    global _access_token, _access_expires_at
    with _access_lock:
        _access_token = token
        _access_expires_at = (time.time() + max(60.0, float(expires_in)) - 60.0) if token else 0.0


def get_access_token_memory() -> Optional[str]:
    with _access_lock:
        if _access_token and time.time() < _access_expires_at:
            return _access_token
        return _access_token if _access_token else None


def parse_aad_error(payload: Any) -> tuple[Optional[str], str]:
    """Extract AADSTS code + plain words from token/HTTP error payload."""
    text = ""
    codes: list[Any] = []
    if isinstance(payload, dict):
        text = str(
            payload.get("error_description")
            or payload.get("error_message")
            or payload.get("message")
            or payload.get("error")
            or ""
        )
        raw_codes = payload.get("error_codes") or payload.get("errorCodes") or []
        if isinstance(raw_codes, list):
            codes = raw_codes
        err = payload.get("error")
        if isinstance(err, str) and err.startswith("AADSTS"):
            return err.split(":")[0].strip(), text or err
    else:
        text = str(payload or "")

    m = re.search(r"(AADSTS\d+)", text)
    if m:
        return m.group(1), text
    if codes:
        code = f"AADSTS{codes[0]}" if not str(codes[0]).startswith("AADSTS") else str(codes[0])
        return code, text or code
    return None, text or "sign-in failed"


def plain_words_for_aad(code: Optional[str]) -> str:
    if code == AAD_USER_DECLINED:
        return "You cancelled or declined sign-in. Nothing was stored."
    if code in {AAD_ADMIN_REQUIRED_94, AAD_ADMIN_REQUIRED_95}:
        return "An admin must approve this app before you can sign in."
    if code == AAD_CONSENT_MISSING:
        return "Consent is missing — open Sign in again and choose Accept."
    if code:
        return f"Sign-in failed ({code})."
    return "Sign-in failed."


def status_from_aad(code: Optional[str]) -> str:
    if code == AAD_USER_DECLINED:
        return STATUS_OFF
    if code in {AAD_ADMIN_REQUIRED_94, AAD_ADMIN_REQUIRED_95, AAD_CONSENT_MISSING}:
        return STATUS_NEEDS_SIGN_IN
    if code:
        return STATUS_NEEDS_SIGN_IN
    return STATUS_OFF


def current_status(
    *,
    identity_hint: Optional[str] = None,
    last_error_code: Optional[str] = None,
    last_error_plain: Optional[str] = None,
) -> dict[str, Any]:
    """Settings status payload — never includes tokens."""
    with _device_lock:
        session = _device_session
        if session and not session.done and not session.cancelled and time.time() < session.expires_at:
            return {
                "status": STATUS_SIGN_IN_IN_PROGRESS,
                "setting_name": REFRESH_TOKEN_ENV,
                "has_refresh_token": has_refresh_token(),
                "revoke_enabled": has_refresh_token(),
                "identity_masked": None,
                "user_code": session.user_code,
                "verification_uri": session.verification_uri or DEVICE_LOGIN_URL,
                "device_message": session.message,
                "error_code": None,
                "error_plain": None,
                "scopes": [{"id": s, "label": lab, "admin_consent": False} for s, lab in _scope_pairs()],
                "client_id_configured": bool(get_client_id()),
            }
        # Finished session may still carry Needs sign-in error until next start.
        if session and session.done and session.error_code and not has_refresh_token():
            return {
                "status": session.status or status_from_aad(session.error_code),
                "setting_name": REFRESH_TOKEN_ENV,
                "has_refresh_token": False,
                "revoke_enabled": False,
                "identity_masked": None,
                "user_code": None,
                "verification_uri": None,
                "device_message": None,
                "error_code": session.error_code,
                "error_plain": session.error_plain or plain_words_for_aad(session.error_code),
                "scopes": [{"id": s, "label": lab, "admin_consent": False} for s, lab in _scope_pairs()],
                "client_id_configured": bool(get_client_id()),
                "null_honest": True,
            }

    token_present = has_refresh_token()
    if token_present:
        status = STATUS_SIGNED_IN
        err_c = last_error_code
        err_p = last_error_plain
    elif last_error_code:
        status = status_from_aad(last_error_code)
        err_c, err_p = last_error_code, last_error_plain or plain_words_for_aad(last_error_code)
    else:
        status = STATUS_OFF
        err_c = err_p = None

    return {
        "status": status,
        "setting_name": REFRESH_TOKEN_ENV,
        "has_refresh_token": token_present,
        "revoke_enabled": token_present,
        "identity_masked": mask_identity(identity_hint) if token_present else None,
        "user_code": None,
        "verification_uri": None,
        "device_message": None,
        "error_code": err_c,
        "error_plain": err_p,
        "scopes": [{"id": s, "label": lab, "admin_consent": False} for s, lab in _scope_pairs()],
        "client_id_configured": bool(get_client_id()),
        "null_honest": not token_present,
    }


def _scope_pairs():
    from plugins.work_watch.constants import SCOPE_PLAIN

    return SCOPE_PLAIN


def revoke() -> dict[str, Any]:
    """Revoke local refresh token. No-op / honest refuse when none exists."""
    if not has_refresh_token():
        return {
            "ok": True,
            "revoked": False,
            "reason": "no_token",
            "status": STATUS_OFF,
            "setting_name": REFRESH_TOKEN_ENV,
            "null_honest": True,
        }
    _clear_access_token()
    removed = _clear_refresh_token()
    cancel_device_flow()
    return {
        "ok": True,
        "revoked": bool(removed),
        "reason": "cleared" if removed else "already_absent",
        "status": STATUS_OFF,
        "setting_name": REFRESH_TOKEN_ENV,
        "null_honest": True,
    }


def cancel_device_flow() -> None:
    global _device_session
    _poller_stop.set()
    with _device_lock:
        if _device_session and not _device_session.done:
            _device_session.cancelled = True
            _device_session.done = True
            _device_session.status = STATUS_OFF
            _device_session.error_code = AAD_USER_DECLINED
            _device_session.error_plain = plain_words_for_aad(AAD_USER_DECLINED)
        _device_session = None


def _spawn_poller_thread(target: Callable[[], None]) -> threading.Thread:
    """AC-P1 / AC-N6: carry profile secret_scope into the device-code poller.

    Bare ``threading.Thread`` drops the contextvar on a multiplex gateway, so a
    successful grant cannot write the owning profile's ``.env``. Prefer
    ``agent.memory_provider.spawn_context_thread`` (Probe defect 2).
    """
    try:
        from agent.memory_provider import spawn_context_thread

        return spawn_context_thread(
            target,
            name="work-watch-device-code",
            daemon=True,
        )
    except Exception as exc:
        logger.warning(
            "work_watch: spawn_context_thread unavailable (%s); bare thread fallback",
            type(exc).__name__,
        )
        t = threading.Thread(target=target, name="work-watch-device-code", daemon=True)
        t.start()
        return t


def start_device_code_sign_in(
    *,
    msal_app_factory: Optional[Callable[..., Any]] = None,  # kept for old call sites; ignored
) -> dict[str, Any]:
    """Start device-code flow over httpx. Returns user_code + verification URI; never tokens.

    ``msal_app_factory`` is accepted but ignored — msal is not used (Probe defect 1).
    """
    global _device_session, _poller_thread
    del msal_app_factory  # explicit: httpx path only

    client_id = get_client_id()
    if not client_id:
        return {
            "ok": False,
            "status": STATUS_NEEDS_SIGN_IN,
            "error_code": "CONFIG",
            "error_plain": f"Set {CLIENT_ID_ENV} to your public-client app id (no secret).",
            "setting_name": REFRESH_TOKEN_ENV,
        }

    # Cancel any prior poller.
    _poller_stop.set()
    if _poller_thread is not None and _poller_thread.is_alive():
        _poller_thread.join(timeout=2.0)
    _poller_stop.clear()

    tenant = get_tenant()
    scope = " ".join(SCOPES)
    try:
        with _http_client() as client:
            resp = client.post(
                _device_code_endpoint(tenant),
                data={"client_id": client_id, "scope": scope},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        logger.warning("work_watch device code request failed: %s", type(exc).__name__)
        return {
            "ok": False,
            "status": STATUS_NEEDS_SIGN_IN,
            "error_code": "DEVICE_CODE_REQUEST_FAILED",
            "error_plain": "Could not reach Microsoft sign-in. Try again.",
            "setting_name": REFRESH_TOKEN_ENV,
        }

    try:
        flow = resp.json() if resp.content else {}
    except Exception:
        flow = {}

    if resp.status_code >= 400 or not isinstance(flow, dict) or "user_code" not in flow:
        code, plain = parse_aad_error(flow if isinstance(flow, dict) else {})
        if not code and resp.status_code >= 400:
            code = f"HTTP_{resp.status_code}"
        return {
            "ok": False,
            "status": status_from_aad(code),
            "error_code": code,
            "error_plain": plain_words_for_aad(code) if code and str(code).startswith("AADSTS") else (
                plain or "device code start failed"
            ),
            "setting_name": REFRESH_TOKEN_ENV,
        }

    device_code = str(flow.get("device_code") or "")
    user_code = str(flow["user_code"])
    if not device_code:
        return {
            "ok": False,
            "status": STATUS_NEEDS_SIGN_IN,
            "error_code": "DEVICE_CODE_INCOMPLETE",
            "error_plain": "Microsoft device-code response was incomplete.",
            "setting_name": REFRESH_TOKEN_ENV,
        }

    expires_in = float(flow.get("expires_in") or 900)
    interval = max(1, int(flow.get("interval") or 5))
    verification = str(
        flow.get("verification_uri")
        or flow.get("verification_uri_complete")
        or DEVICE_LOGIN_URL
    )
    if "devicelogin" not in verification.lower():
        verification = DEVICE_LOGIN_URL
    message = str(flow.get("message") or f"Go to {verification} and enter {user_code}")

    session = DeviceCodeSession(
        user_code=user_code,
        verification_uri=verification,
        message=message,
        expires_at=time.time() + expires_in,
        flow={
            "device_code": device_code,
            "client_id": client_id,
            "tenant": tenant,
            "interval": interval,
            "user_code": user_code,
        },
    )
    with _device_lock:
        _device_session = session

    def _poll() -> None:
        _poll_device_flow_httpx(session)

    _poller_thread = _spawn_poller_thread(_poll)

    return {
        "ok": True,
        "status": STATUS_SIGN_IN_IN_PROGRESS,
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "message": session.message,
        "expires_in": int(expires_in),
        "setting_name": REFRESH_TOKEN_ENV,
    }


def _poll_device_flow_httpx(session: DeviceCodeSession) -> None:
    """Poll token endpoint until success, cancel, or error. Never writes access token to disk."""
    flow = session.flow or {}
    device_code = str(flow.get("device_code") or "")
    client_id = str(flow.get("client_id") or get_client_id())
    tenant = str(flow.get("tenant") or get_tenant())
    interval = max(1, int(flow.get("interval") or 5))
    deadline = session.expires_at

    while not _poller_stop.is_set() and time.time() < deadline:
        with _device_lock:
            if session.cancelled or session.done:
                return

        try:
            with _http_client() as client:
                resp = client.post(
                    _token_endpoint(tenant),
                    data={
                        "grant_type": _DEVICE_CODE_GRANT,
                        "client_id": client_id,
                        "device_code": device_code,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except Exception as exc:
            logger.warning("work_watch device poll transport: %s", type(exc).__name__)
            time.sleep(interval)
            continue

        try:
            result = resp.json() if resp.content else {}
        except Exception:
            time.sleep(interval)
            continue
        if not isinstance(result, dict):
            time.sleep(interval)
            continue

        with _device_lock:
            if session.cancelled:
                # AC-N3: cancel wins — no partial session stored.
                return

        if resp.status_code == 200 and result.get("refresh_token"):
            try:
                _save_refresh_token(str(result["refresh_token"]))
            except Exception as exc:
                logger.warning(
                    "work_watch: failed to persist refresh token: %s", type(exc).__name__
                )
                with _device_lock:
                    session.done = True
                    session.status = STATUS_NEEDS_SIGN_IN
                    session.error_code = "PERSIST_FAILED"
                    session.error_plain = "could not store refresh token"
                return
            # Access token memory-only.
            if result.get("access_token"):
                set_access_token_memory(
                    str(result["access_token"]),
                    float(result.get("expires_in") or 3600),
                )
            claims = result.get("id_token_claims") or {}
            if not claims and result.get("id_token"):
                claims = _decode_id_token_claims_unverified(str(result["id_token"]))
            upn = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
            if upn:
                try:
                    from hermes_cli.config import save_env_value

                    save_env_value("GRAPH_WATCH_IDENTITY_MASKED", mask_identity(str(upn)) or "")
                except Exception:
                    pass
            with _device_lock:
                session.done = True
                session.status = STATUS_SIGNED_IN
                session.error_code = None
                session.error_plain = None
            return

        err = str(result.get("error") or "").lower()
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval = min(interval + 1, 30)
            time.sleep(interval)
            continue
        if err in {"authorization_declined", "access_denied", "expired_token"}:
            code, plain = parse_aad_error(result)
            if err in {"authorization_declined", "access_denied"} or code == AAD_USER_DECLINED:
                code = AAD_USER_DECLINED
            with _device_lock:
                session.done = True
                session.error_code = code or AAD_USER_DECLINED
                session.error_plain = plain_words_for_aad(session.error_code)
                session.status = status_from_aad(session.error_code)
            _clear_access_token()
            return

        code, plain = parse_aad_error(result)
        with _device_lock:
            session.done = True
            session.error_code = code
            session.error_plain = plain_words_for_aad(code) if code else plain
            session.status = status_from_aad(code)
        _clear_access_token()
        return

    with _device_lock:
        if not session.done:
            session.done = True
            if session.cancelled or _poller_stop.is_set():
                session.status = STATUS_OFF
                session.error_code = AAD_USER_DECLINED
                session.error_plain = plain_words_for_aad(AAD_USER_DECLINED)
            else:
                session.status = STATUS_NEEDS_SIGN_IN
                session.error_code = "DEVICE_CODE_EXPIRED"
                session.error_plain = "The sign-in code expired. Try Sign in again."
    _clear_access_token()


def _decode_id_token_claims_unverified(id_token: str) -> dict[str, Any]:
    """Best-effort JWT payload decode for display mask only — not for auth decisions."""
    try:
        import base64
        import json

        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def finish_device_flow_for_tests(
    *,
    refresh_token: Optional[str] = None,
    error_code: Optional[str] = None,
    cancelled: bool = False,
) -> AuthOutcome:
    """Test helper: complete or fail the in-flight session without network."""
    global _device_session
    with _device_lock:
        session = _device_session
        if cancelled:
            cancel_device_flow()
            return AuthOutcome(
                ok=False,
                status=STATUS_OFF,
                error_code=AAD_USER_DECLINED,
                error_plain=plain_words_for_aad(AAD_USER_DECLINED),
                has_refresh_token=False,
            )
        if error_code:
            if session:
                session.done = True
                session.error_code = error_code
                session.error_plain = plain_words_for_aad(error_code)
                session.status = status_from_aad(error_code)
            return AuthOutcome(
                ok=False,
                status=status_from_aad(error_code),
                error_code=error_code,
                error_plain=plain_words_for_aad(error_code),
                has_refresh_token=has_refresh_token(),
            )
        if refresh_token:
            _save_refresh_token(refresh_token)
            if session:
                session.done = True
                session.status = STATUS_SIGNED_IN
            return AuthOutcome(ok=True, status=STATUS_SIGNED_IN, has_refresh_token=True)
    return AuthOutcome(ok=False, status=STATUS_OFF, has_refresh_token=has_refresh_token())


def acquire_access_token(
    *,
    msal_app_factory: Optional[Callable[..., Any]] = None,  # ignored; httpx only
    force_refresh: bool = False,
) -> Optional[str]:
    """Return a memory-only access token, refreshing via stored refresh token over httpx.

    On hard failure (refresh refused) returns None and does not invent success.
    Caller stops the reader (AC-N9).
    """
    del msal_app_factory
    if not force_refresh:
        cached = get_access_token_memory()
        if cached:
            return cached
    if not has_refresh_token():
        return None
    client_id = get_client_id()
    if not client_id:
        return None

    rt = _getenv(REFRESH_TOKEN_ENV)
    if not rt:
        return None

    try:
        with _http_client() as client:
            resp = client.post(
                _token_endpoint(),
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": rt,
                    "scope": " ".join(SCOPES),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        logger.warning("work_watch token refresh transport failed: %s", type(exc).__name__)
        _clear_access_token()
        return None

    try:
        result = resp.json() if resp.content else {}
    except Exception:
        _clear_access_token()
        return None

    if not isinstance(result, dict) or resp.status_code >= 400 or not result.get("access_token"):
        _clear_access_token()
        return None

    # Rotate refresh token if the IdP returns a new one.
    if result.get("refresh_token"):
        try:
            _save_refresh_token(str(result["refresh_token"]))
        except Exception:
            pass
    set_access_token_memory(str(result["access_token"]), float(result.get("expires_in") or 3600))
    return str(result["access_token"])


def reader_may_call_graph() -> bool:
    """AC-N1: zero Graph calls when refresh token absent/NULL."""
    return has_refresh_token()
