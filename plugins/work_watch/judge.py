"""Rules-first local judge for Work watch (Path A).

Cloud model is a separate switch (default off). Message content never leaves
the machine through this module's public board-facing outputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from plugins.work_watch.constants import TIER_1, TIER_2, TIER_3

# Newsletter / marketing / no-action automated patterns (subject + sender heuristics).
_TIER3_SUBJECT = re.compile(
    r"("
    r"unsubscribe|newsletter|weekly\s+digest|daily\s+digest|marketing|"
    r"promotion|% off|sale ends|your\s+receipt\s+from|nomail|"
    r"noreply|no-reply|do[- ]not[- ]reply|mailer-daemon|automated\s+message|"
    r"view\s+in\s+browser|this\s+email\s+was\s+sent\s+to"
    r")",
    re.IGNORECASE,
)
_TIER3_SENDER = re.compile(
    r"("
    r"noreply@|no-reply@|donotreply@|newsletter@|marketing@|notifications@|"
    r"mailer-daemon@|updates@.*\.(com|io|co)$"
    r")",
    re.IGNORECASE,
)

# Tier-1: direct ask / blocker / needs him.
_TIER1 = re.compile(
    r"("
    r"\b(need(s)?\s+you|action\s+required|please\s+(review|approve|sign|confirm)|"
    r"blocker|blocked|urgent|asap|deadline|sign[- ]off|your\s+input|"
    r"waiting\s+on\s+you|can\s+you\s+(look|review|help|confirm))\b"
    r")",
    re.IGNORECASE,
)

# Tier-2: FYI / thread update worth a digest line.
_TIER2 = re.compile(
    r"("
    r"\b(fyi|heads[- ]up|update:|status:|following\s+up|reminder|"
    r"meeting\s+notes|summary\s+of|for\s+your\s+awareness)\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Judgment:
    tier: int
    why: str
    rule: str  # which rule fired
    dropped: bool = False


def _blob(candidate: dict[str, Any]) -> str:
    parts = [
        str(candidate.get("subject") or ""),
        str(candidate.get("sender") or ""),
        str(candidate.get("preview") or ""),
        str(candidate.get("body_preview") or ""),
    ]
    return "\n".join(parts)


def classify(candidate: dict[str, Any], *, cloud_enabled: bool = False) -> Judgment:
    """Classify one mail/chat candidate. Rules first; no cloud by default.

    ``cloud_enabled`` is accepted for the explicit switch path but Path A default
    keeps it off — ambiguous middle stays Tier-2 digest rather than calling out.
    """
    subject = str(candidate.get("subject") or "")
    sender = str(candidate.get("sender") or "")
    text = _blob(candidate)

    # Tier-3 first (quiet by default).
    if _TIER3_SENDER.search(sender) or _TIER3_SUBJECT.search(subject) or _TIER3_SUBJECT.search(text):
        why = "newsletter_or_automated"
        if _TIER3_SENDER.search(sender):
            why = "automated_sender"
        elif re.search(r"unsubscribe|newsletter|marketing|% off|sale ends", subject, re.IGNORECASE):
            why = "newsletter_or_marketing"
        return Judgment(tier=TIER_3, why=why, rule="tier3_rules", dropped=True)

    if _TIER1.search(text):
        m = _TIER1.search(text)
        snippet = (m.group(0) if m else "action").strip()
        return Judgment(tier=TIER_1, why=f"direct_ask:{snippet.lower()[:40]}", rule="tier1_rules")

    if _TIER2.search(text):
        m = _TIER2.search(text)
        snippet = (m.group(0) if m else "fyi").strip()
        return Judgment(tier=TIER_2, why=f"digest:{snippet.lower()[:40]}", rule="tier2_rules")

    # Ambiguous middle: local default = Tier-2 quiet digest tag, not cloud.
    # A future local-model hook can replace this without changing the contract.
    if cloud_enabled:
        # Explicit switch only — still no content export from here; caller owns model I/O.
        return Judgment(tier=TIER_2, why="ambiguous_cloud_pending", rule="cloud_switch")

    # Nothing matching → drop as noise (AC-N16 quiet when nothing important).
    return Judgment(tier=TIER_3, why="no_action_signal", rule="default_quiet", dropped=True)


def apply_to_store(conn, candidate: dict[str, Any], judgment: Judgment) -> Optional[dict[str, Any]]:
    """Write flag or tier-3 receipt. Returns board-safe flag dict or None if dropped."""
    from plugins.work_watch import store as st

    cid = str(candidate.get("conversation_id") or candidate.get("id") or "").strip()
    if not cid:
        raise ValueError("conversation_id required")
    source = str(candidate.get("source") or "mail")

    if judgment.dropped or judgment.tier == TIER_3:
        st.drop_tier3(conn, conversation_id=cid, why=judgment.why)
        return None

    row = st.upsert_flag(
        conn,
        conversation_id=cid,
        source=source,
        tier=judgment.tier,
        why=judgment.why,
        title_local=str(candidate.get("subject") or "")[:200] or None,
        preview_local=str(candidate.get("preview") or candidate.get("body_preview") or "")[:280] or None,
        sender_local=str(candidate.get("sender") or "")[:200] or None,
        item_id=str(candidate.get("id") or "") or None,
    )
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "source": row.source,
        "tier": row.tier,
        "why": row.why,
        "updated_at": row.updated_at,
    }
