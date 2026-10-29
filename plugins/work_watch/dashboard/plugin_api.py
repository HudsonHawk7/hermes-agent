"""Work watch dashboard API — mounted at /api/plugins/work_watch/."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


class EmptyBody(BaseModel):
    pass


@router.get("/status")
def get_status() -> dict[str, Any]:
    from plugins.work_watch import service

    return service.settings_snapshot()


@router.get("/flags")
def get_flags(local: bool = False) -> dict[str, Any]:
    """List flags. ``local=1`` includes local-only title/preview (never default for bus)."""
    from plugins.work_watch import store

    with store.open_store() as conn:
        flags = store.list_flags(conn, include_local_content=bool(local))
        summary = store.board_summary(conn)
        last = store.last_check(conn)
    return {"flags": flags, "summary": summary, "last_check": last}


@router.get("/receipts/tier3")
def get_tier3_receipts(limit: int = 50) -> dict[str, Any]:
    from plugins.work_watch import store

    with store.open_store() as conn:
        rows = store.list_tier3_receipts(conn, limit=limit)
    return {"receipts": rows}


@router.post("/sign-in")
def post_sign_in() -> dict[str, Any]:
    """Start device-code flow (AC-P1)."""
    from plugins.work_watch import auth

    result = auth.start_device_code_sign_in()
    if not result.get("ok") and result.get("error_code") == "CONFIG":
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/sign-in/cancel")
def post_sign_in_cancel() -> dict[str, Any]:
    from plugins.work_watch import auth

    auth.cancel_device_flow()
    return {"ok": True, "status": "off", "cancelled": True}


@router.post("/revoke")
def post_revoke() -> dict[str, Any]:
    """Revoke local refresh token (AC-N2, AC-N8)."""
    from plugins.work_watch import auth, service

    out = auth.revoke()
    service.clear_reader_stop()
    return out


@router.post("/poll")
def post_poll() -> dict[str, Any]:
    """Manual one-shot poll (also used by tests)."""
    from plugins.work_watch import service

    return service.run_once()


@router.get("/health")
def health() -> dict[str, str]:
    return {"ok": "true", "plugin": "work_watch"}
