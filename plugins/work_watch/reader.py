"""Graph reader — mail delta + chat reads. Zero calls when unsigned (AC-N1)."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from plugins.work_watch import auth
from plugins.work_watch.constants import GRAPH_BASE

logger = logging.getLogger(__name__)

HttpGet = Callable[[str, dict[str, str], Optional[dict[str, Any]]], dict[str, Any]]


class ReaderStopped(RuntimeError):
    """Raised when auth fails hard — caller must stop and show Needs sign-in (AC-N9)."""


class _Http401(Exception):
    """Internal: Graph returned 401; caller may refresh once and retry."""


def _default_http_get(url: str, headers: dict[str, str], params: Optional[dict[str, Any]]) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url, headers=headers, params=params)
        if resp.status_code == 401:
            raise _Http401("graph_401")
        if resp.status_code >= 400:
            raise RuntimeError(f"graph_http_{resp.status_code}")
        return resp.json() if resp.content else {}


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        # Prefer minimal projections — still local-only after fetch.
        "Prefer": 'outlook.body-content-type="text"',
    }


def _get_with_401_retry(
    getter: HttpGet,
    url: str,
    headers: dict[str, str],
    params: Optional[dict[str, Any]],
    *,
    token_provider: Callable[[], Optional[str]],
    force_refresh: Callable[[], Optional[str]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """One Graph GET with a single 401 → refresh → retry (Probe defect 5 / AC-N9).

    Returns (payload, headers_used). A second 401 (or refresh failure) raises
    ``ReaderStopped`` so the service stops and asks for sign-in again.
    """
    try:
        return getter(url, headers, params), headers
    except _Http401:
        pass
    except ReaderStopped as exc:
        # Tests may raise ReaderStopped directly from a fake getter on first 401.
        if "401" not in str(exc).lower() and "graph_401" not in str(exc).lower():
            raise
    # Refresh once.
    auth.set_access_token_memory(None)
    token2 = force_refresh() if force_refresh else token_provider()
    if not token2:
        raise ReaderStopped("401_after_refresh")
    headers2 = _auth_headers(token2)
    try:
        return getter(url, headers2, params), headers2
    except (_Http401, ReaderStopped):
        raise ReaderStopped("401_retry_exhausted") from None


def fetch_candidates(
    *,
    http_get: Optional[HttpGet] = None,
    token_provider: Optional[Callable[[], Optional[str]]] = None,
    force_refresh: Optional[Callable[[], Optional[str]]] = None,
    mail_delta_link: Optional[str] = None,
    max_mail: int = 50,
    max_chats: int = 20,
) -> dict[str, Any]:
    """Pull mail delta + recent chats. Returns candidates + next delta link.

    Never called when ``auth.reader_may_call_graph()`` is False.
    Discovers Teams chats via ``GET /me/chats`` then latest message per chat (AC-P4).
    """
    if not auth.reader_may_call_graph():
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_refresh_token",
            "candidates": [],
            "graph_calls": 0,
            "mail_delta_link": mail_delta_link,
        }

    get_token = token_provider or (lambda: auth.acquire_access_token())
    refresh = force_refresh or (lambda: auth.acquire_access_token(force_refresh=True))
    token = get_token()
    if not token:
        # AC-N9: stop — do not spin.
        raise ReaderStopped("refresh_failed")

    getter = http_get or _default_http_get
    headers = _auth_headers(token)
    calls = 0
    candidates: list[dict[str, Any]] = []
    next_delta = mail_delta_link

    # Mail delta
    if mail_delta_link:
        url = mail_delta_link
        params = None
    else:
        url = f"{GRAPH_BASE}/me/mailFolders('inbox')/messages/delta"
        params = {
            "$top": str(max_mail),
            "$select": "id,conversationId,subject,from,bodyPreview,receivedDateTime",
        }
    payload, headers = _get_with_401_retry(
        getter, url, headers, params, token_provider=get_token, force_refresh=refresh
    )
    calls += 1
    next_delta = payload.get("@odata.deltaLink") or payload.get("@odata.nextLink") or mail_delta_link
    for item in payload.get("value") or []:
        if not isinstance(item, dict):
            continue
        if item.get("@removed"):
            continue
        frm = item.get("from") or {}
        addr = ""
        if isinstance(frm, dict):
            ea = frm.get("emailAddress") or {}
            addr = str(ea.get("address") or ea.get("name") or "")
        candidates.append(
            {
                "id": str(item.get("id") or ""),
                "conversation_id": str(item.get("conversationId") or item.get("id") or ""),
                "source": "mail",
                "subject": str(item.get("subject") or ""),
                "sender": addr,
                "preview": str(item.get("bodyPreview") or ""),
                "body_preview": str(item.get("bodyPreview") or ""),
                "received_at": item.get("receivedDateTime"),
            }
        )

    # Chats (Chat.Read) — list recent chats then latest message per chat when no delta.
    try:
        chats_url = f"{GRAPH_BASE}/me/chats"
        chats, headers = _get_with_401_retry(
            getter,
            chats_url,
            headers,
            {"$top": str(max_chats), "$select": "id,topic,lastUpdatedDateTime"},
            token_provider=get_token,
            force_refresh=refresh,
        )
        calls += 1
        for chat in (chats.get("value") or [])[:max_chats]:
            if not isinstance(chat, dict) or not chat.get("id"):
                continue
            cid = str(chat["id"])
            msg_url = f"{GRAPH_BASE}/me/chats/{cid}/messages"
            try:
                msgs, headers = _get_with_401_retry(
                    getter,
                    msg_url,
                    headers,
                    {"$top": "1", "$orderby": "createdDateTime desc"},
                    token_provider=get_token,
                    force_refresh=refresh,
                )
                calls += 1
            except ReaderStopped:
                raise
            except Exception:
                continue
            for msg in msgs.get("value") or []:
                if not isinstance(msg, dict):
                    continue
                from_user = ((msg.get("from") or {}).get("user") or {})
                sender = str(from_user.get("displayName") or from_user.get("id") or "")
                body = msg.get("body") or {}
                preview = str(body.get("content") or "")[:280]
                candidates.append(
                    {
                        "id": str(msg.get("id") or ""),
                        "conversation_id": cid,
                        "source": "chat",
                        "subject": str(chat.get("topic") or "Teams chat"),
                        "sender": sender,
                        "preview": preview,
                        "body_preview": preview,
                        "received_at": msg.get("createdDateTime"),
                    }
                )
    except ReaderStopped:
        raise
    except Exception as exc:
        logger.debug("work_watch chat read degraded: %s", type(exc).__name__)

    return {
        "ok": True,
        "skipped": False,
        "candidates": candidates,
        "graph_calls": calls,
        "mail_delta_link": next_delta,
        "fetched_at": time.time(),
        "chats_discovered": sum(1 for c in candidates if c.get("source") == "chat"),
    }
