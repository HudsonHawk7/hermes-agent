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


def _default_http_get(url: str, headers: dict[str, str], params: Optional[dict[str, Any]]) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url, headers=headers, params=params)
        if resp.status_code == 401:
            raise ReaderStopped("graph_401")
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


def fetch_candidates(
    *,
    http_get: Optional[HttpGet] = None,
    token_provider: Optional[Callable[[], Optional[str]]] = None,
    mail_delta_link: Optional[str] = None,
    max_mail: int = 50,
    max_chats: int = 20,
) -> dict[str, Any]:
    """Pull mail delta + recent chats. Returns candidates + next delta link.

    Never called when ``auth.reader_may_call_graph()`` is False.
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
    try:
        if mail_delta_link:
            url = mail_delta_link
            params = None
        else:
            url = f"{GRAPH_BASE}/me/mailFolders('inbox')/messages/delta"
            params = {
                "$top": str(max_mail),
                "$select": "id,conversationId,subject,from,bodyPreview,receivedDateTime",
            }
        payload = getter(url, headers, params)
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
    except ReaderStopped:
        # One refresh+retry
        auth.set_access_token_memory(None)
        token2 = get_token()
        if not token2:
            raise ReaderStopped("401_after_refresh")
        # Let caller retry whole pass; signal stop.
        raise ReaderStopped("401_retry_exhausted")

    # Chats (Chat.Read) — list recent chats then latest message per chat when no delta.
    try:
        chats_url = f"{GRAPH_BASE}/me/chats"
        chats = getter(chats_url, headers, {"$top": str(max_chats), "$select": "id,topic,lastUpdatedDateTime"})
        calls += 1
        for chat in (chats.get("value") or [])[:max_chats]:
            if not isinstance(chat, dict) or not chat.get("id"):
                continue
            cid = str(chat["id"])
            msg_url = f"{GRAPH_BASE}/me/chats/{cid}/messages"
            try:
                msgs = getter(msg_url, headers, {"$top": "1", "$orderby": "createdDateTime desc"})
                calls += 1
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
    }
