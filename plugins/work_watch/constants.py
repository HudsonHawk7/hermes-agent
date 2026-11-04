"""Work watch Path A constants (no secrets)."""

from __future__ import annotations

PLUGIN_NAME = "work_watch"
PLUGIN_LABEL = "Work mail + Teams"

# Profile .env keys — refresh token only at rest; access token never written.
REFRESH_TOKEN_ENV = "GRAPH_WATCH_REFRESH_TOKEN"
CLIENT_ID_ENV = "GRAPH_WATCH_CLIENT_ID"
TENANT_ENV = "GRAPH_WATCH_TENANT"
CLOUD_JUDGE_ENV = "GRAPH_WATCH_CLOUD_JUDGE"  # explicit opt-in; default off

# Public-client delegated scopes (no admin consent expected for personal use).
SCOPES: tuple[str, ...] = (
    "offline_access",
    "User.Read",
    "Mail.Read",
    "Chat.Read",
)

SCOPE_PLAIN: tuple[tuple[str, str], ...] = (
    ("offline_access", "Stay signed in"),
    ("User.Read", "Who you are"),
    ("Mail.Read", "Read your inbox"),
    ("Chat.Read", "Read your Teams chats"),
)

DEVICE_LOGIN_URL = "https://microsoft.com/devicelogin"
DEFAULT_AUTHORITY_TENANT = "organizations"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Status wire values (Settings contract).
STATUS_OFF = "off"
STATUS_SIGNED_IN = "signed_in"
STATUS_SIGN_IN_IN_PROGRESS = "sign_in_in_progress"
STATUS_NEEDS_SIGN_IN = "needs_sign_in"

# AAD error codes we surface honestly (design §5).
AAD_CONSENT_MISSING = "AADSTS65001"
AAD_USER_DECLINED = "AADSTS65004"
AAD_ADMIN_REQUIRED_94 = "AADSTS90094"
AAD_ADMIN_REQUIRED_95 = "AADSTS90095"

# Poll cadence (CT windows applied in service).
POLL_ACTIVE_MINUTES = 10
POLL_OFFHOURS_MINUTES = 60
ACTIVE_HOUR_START_CT = 7
ACTIVE_HOUR_END_CT = 19
DIGEST_HOUR_CT = 8
DIGEST_MINUTE_CT = 7

# Tiers
TIER_1 = 1  # Work watch + optional phone push
TIER_2 = 2  # digest only
TIER_3 = 3  # drop (newsletter / marketing / no-action automated)

# Masked identity shape for UI (never full address in receipts).
IDENTITY_MASK_RE = r"^([^@]{0,1})[^@]*(@.+)$"
