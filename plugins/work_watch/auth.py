"""Device-code sign-in + revoke for Work watch (public client, no secret).

Refresh token lives in the profile ``.env`` as ``GRAPH_WATCH_REFRESH_TOKEN`` via
``save_env_value`` / ``remove_env_value``. Access tokens stay in-process memory only.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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

# In-memory only — never written to disk/logs/cards.
_access_token: Optional[str] = None
_access_expires_at: float = 0.0
_access_lock = threading.Lock()

# Active device-code session (one at a time). RLock so finish/cancel helpers can nest.
_device_lock = threading.RLock()
_device_session: Optional[DeviceCodeSession] = None


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
    """Extract AADSTS code + plain words from MSAL/HTTP error payload."""
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
    with _device_lock:
        if _device_session and not _device_session.done:
            _device_session.cancelled = True
            _device_session.done = True
            _device_session.status = STATUS_OFF
            _device_session.error_code = AAD_USER_DECLINED
            _device_session.error_plain = plain_words_for_aad(AAD_USER_DECLINED)
        _device_session = None


def start_device_code_sign_in(
    *,
    msal_app_factory: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Start device-code flow. Returns user_code + verification URI; never tokens."""
    client_id = get_client_id()
    if not client_id:
        return {
            "ok": False,
            "status": STATUS_NEEDS_SIGN_IN,
            "error_code": "CONFIG",
            "error_plain": f"Set {CLIENT_ID_ENV} to your public-client app id (no secret).",
            "setting_name": REFRESH_TOKEN_ENV,
        }

    try:
        import msal
    except ImportError as exc:  # pragma: no cover
        return {
            "ok": False,
            "status": STATUS_NEEDS_SIGN_IN,
            "error_code": "MSAL_MISSING",
            "error_plain": f"msal is required for device-code sign-in: {exc}",
        }

    authority = f"https://login.microsoftonline.com/{get_tenant()}"
    factory = msal_app_factory or (
        lambda: msal.PublicClientApplication(client_id, authority=authority)
    )
    app = factory()
    flow = app.initiate_device_flow(scopes=list(SCOPES))
    if "user_code" not in flow:
        code, plain = parse_aad_error(flow)
        return {
            "ok": False,
            "status": status_from_aad(code),
            "error_code": code,
            "error_plain": plain_words_for_aad(code) if code else (plain or "device code start failed"),
            "setting_name": REFRESH_TOKEN_ENV,
        }

    expires_in = float(flow.get("expires_in") or 900)
    session = DeviceCodeSession(
        user_code=str(flow["user_code"]),
        verification_uri=str(flow.get("verification_uri") or flow.get("verification_uri_complete") or DEVICE_LOGIN_URL),
        message=str(flow.get("message") or f"Go to {DEVICE_LOGIN_URL} and enter the code."),
        expires_at=time.time() + expires_in,
        flow=flow,
    )
    with _device_lock:
        global _device_session
        _device_session = session

    # Background poll — stores refresh token only on success.
    thread = threading.Thread(
        target=_poll_device_flow,
        args=(app, session),
        name="work-watch-device-code",
        daemon=True,
    )
    thread.start()

    return {
        "ok": True,
        "status": STATUS_SIGN_IN_IN_PROGRESS,
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "message": session.message,
        "expires_in": int(expires_in),
        "setting_name": REFRESH_TOKEN_ENV,
    }


def _poll_device_flow(app: Any, session: DeviceCodeSession) -> None:
    """Poll MSAL until success, cancel, or error. Never writes access token."""
    try:
        if session.cancelled:
            return
        result = app.acquire_token_by_device_flow(session.flow)
        with _device_lock:
            if session.cancelled:
                # AC-N3: cancel wins — no partial session stored.
                return
            if not isinstance(result, dict):
                session.done = True
                session.status = STATUS_NEEDS_SIGN_IN
                session.error_plain = "unexpected token response"
                return
            if result.get("access_token") and result.get("refresh_token"):
                # Persist refresh only.
                try:
                    _save_refresh_token(result["refresh_token"])
                except Exception as exc:
                    logger.warning("work_watch: failed to persist refresh token: %s", type(exc).__name__)
                    session.done = True
                    session.status = STATUS_NEEDS_SIGN_IN
                    session.error_plain = "could not store refresh token"
                    return
                # Access token memory-only.
                set_access_token_memory(
                    result.get("access_token"),
                    float(result.get("expires_in") or 3600),
                )
                # Optional identity from id_token_claims — masked only in status.
                claims = result.get("id_token_claims") or {}
                upn = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
                if upn:
                    try:
                        from hermes_cli.config import save_env_value

                        # Store masked hint only for Settings display — not a secret.
                        save_env_value("GRAPH_WATCH_IDENTITY_MASKED", mask_identity(str(upn)) or "")
                    except Exception:
                        pass
                session.done = True
                session.status = STATUS_SIGNED_IN
                return

            code, plain = parse_aad_error(result)
            # User declined / cancel
            if code == AAD_USER_DECLINED or result.get("error") in {"authorization_pending", "access_denied"}:
                # authorization_pending shouldn't finish; MSAL only returns when done.
                if result.get("error") == "authorization_pending":
                    return
            session.done = True
            session.error_code = code
            session.error_plain = plain_words_for_aad(code) if code else plain
            session.status = status_from_aad(code)
            # AC-N3/N4/N5: token stays NULL — do not write anything.
    except Exception as exc:
        with _device_lock:
            session.done = True
            session.status = STATUS_NEEDS_SIGN_IN
            session.error_plain = f"device flow error: {type(exc).__name__}"
            logger.warning("work_watch device flow failed: %s", type(exc).__name__)


def finish_device_flow_for_tests(
    *,
    refresh_token: Optional[str] = None,
    error_code: Optional[str] = None,
    cancelled: bool = False,
) -> AuthOutcome:
    """Test helper: complete or fail the in-flight session without MSAL."""
    global _device_session
    with _device_lock:
        session = _device_session
        if cancelled:
            cancel_device_flow()
            return AuthOutcome(ok=False, status=STATUS_OFF, error_code=AAD_USER_DECLINED,
                               error_plain=plain_words_for_aad(AAD_USER_DECLINED), has_refresh_token=False)
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
    msal_app_factory: Optional[Callable[..., Any]] = None,
    force_refresh: bool = False,
) -> Optional[str]:
    """Return a memory-only access token, refreshing via stored refresh token.

    On hard failure (refresh refused / 401 after retry path) returns None and
    does not invent success. Caller stops the reader (AC-N9).
    """
    if not force_refresh:
        cached = get_access_token_memory()
        if cached:
            return cached
    if not has_refresh_token():
        return None
    client_id = get_client_id()
    if not client_id:
        return None
    try:
        import msal
    except ImportError:
        return None

    authority = f"https://login.microsoftonline.com/{get_tenant()}"
    factory = msal_app_factory or (
        lambda: msal.PublicClientApplication(client_id, authority=authority)
    )
    app = factory()
    result = app.acquire_token_by_refresh_token(_getenv(REFRESH_TOKEN_ENV), scopes=list(SCOPES))
    if not isinstance(result, dict) or not result.get("access_token"):
        _clear_access_token()
        return None
    # Rotate refresh token if MSAL returns a new one.
    if result.get("refresh_token"):
        try:
            _save_refresh_token(result["refresh_token"])
        except Exception:
            pass
    set_access_token_memory(result["access_token"], float(result.get("expires_in") or 3600))
    return result["access_token"]


def reader_may_call_graph() -> bool:
    """AC-N1: zero Graph calls when refresh token absent/NULL."""
    return has_refresh_token()
