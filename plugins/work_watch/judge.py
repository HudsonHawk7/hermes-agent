"""Rules-first local judge for Work watch (Path A).

Cloud model is a separate switch (default off). Message content never leaves
the machine through this module's public board-facing outputs.

Ambiguous middle uses an optional local-model hook (AC-P7) — one call per
candidate, capped per pass — never a hosted model unless cloud_enabled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

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

# Local-model middle hook (test seam + optional runtime). Returns Judgment or None.
_local_model_fn: Optional[Callable[[dict[str, Any]], Optional[Judgment]]] = None
_local_model_calls_this_pass = 0
_LOCAL_MODEL_CAP = 5  # one model call per candidate, capped per pass (AC-P7)


@dataclass(frozen=True)
class Judgment:
    tier: int
    why: str
    rule: str  # which rule fired
    dropped: bool = False


def set_local_model_fn(fn: Optional[Callable[[dict[str, Any]], Optional[Judgment]]]) -> None:
    """Install/clear the local-model middle hook (AC-P7). Never a cloud default."""
    global _local_model_fn, _local_model_calls_this_pass
    _local_model_fn = fn
    _local_model_calls_this_pass = 0


def reset_local_model_budget() -> None:
    global _local_model_calls_this_pass
    _local_model_calls_this_pass = 0


def local_model_calls() -> int:
    return _local_model_calls_this_pass


def _production_local_model(candidate: dict[str, Any]) -> Optional[Judgment]:
    """AC-P7 production middle: one local attempt, else honest model_unavailable.

    Never returns None — a bare None would fall through to Tier-3 ``no_action_signal``
    and drop ambiguous mail (Probe r3 defect 3). Cloud is a separate switch.
    Content stays on-box: we only call a client whose base_url is clearly local.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
    except Exception:
        return Judgment(
            tier=TIER_2,
            why="model_unavailable",
            rule="local_model_unavailable",
            dropped=False,
        )

    try:
        client, model = get_text_auxiliary_client(task="work_watch_judge")
    except Exception:
        client, model = None, None

    if client is None or not model:
        return Judgment(
            tier=TIER_2,
            why="model_unavailable",
            rule="local_model_unavailable",
            dropped=False,
        )

    # Refuse cloud endpoints for the default middle (cloud is GRAPH_WATCH_CLOUD_JUDGE).
    base = ""
    try:
        base = str(getattr(client, "base_url", "") or getattr(getattr(client, "_client", None), "base_url", "") or "")
    except Exception:
        base = ""
    base_l = base.lower()
    local_hints = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "ollama", "11434", "local")
    if base and not any(h in base_l for h in local_hints):
        return Judgment(
            tier=TIER_2,
            why="model_unavailable",
            rule="local_model_unavailable",
            dropped=False,
        )

    # Minimal local classify — subject/preview only; never export off-box.
    subject = str(candidate.get("subject") or "")[:200]
    preview = str(candidate.get("preview") or candidate.get("body_preview") or "")[:280]
    prompt = (
        "Classify this work message as tier 1 (action needed), 2 (fyi digest), or 3 (drop noise). "
        "Reply with exactly: TIER <n> WHY <short_tag>\n"
        f"Subject: {subject}\nPreview: {preview}\n"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0,
        )
        text = ""
        try:
            text = str(resp.choices[0].message.content or "")
        except Exception:
            text = str(resp)
    except Exception:
        return Judgment(
            tier=TIER_2,
            why="ambiguous_local_error",
            rule="local_model_error",
            dropped=False,
        )

    m = re.search(r"TIER\s*([123]).*?WHY\s*([a-z0-9_:\-]{1,40})", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return Judgment(
            tier=TIER_2,
            why="local:unparsed",
            rule="local_model",
            dropped=False,
        )
    tier = int(m.group(1))
    why = f"local:{m.group(2).lower()}"
    return Judgment(
        tier=tier,
        why=why,
        rule="local_model",
        dropped=(tier == TIER_3),
    )


def install_production_local_model() -> None:
    """Wire the default local-model middle for live register() (AC-P7 / Probe r3)."""
    set_local_model_fn(_production_local_model)
    reset_local_model_budget()


def _blob(candidate: dict[str, Any]) -> str:
    parts = [
        str(candidate.get("subject") or ""),
        str(candidate.get("sender") or ""),
        str(candidate.get("preview") or ""),
        str(candidate.get("body_preview") or ""),
    ]
    return "\n".join(parts)


def _try_local_model(candidate: dict[str, Any]) -> Optional[Judgment]:
    """Ambiguous middle → local model only (AC-P7). Capped; content stays local."""
    global _local_model_calls_this_pass
    if _local_model_fn is None:
        return None
    if _local_model_calls_this_pass >= _LOCAL_MODEL_CAP:
        return Judgment(
            tier=TIER_2,
            why="ambiguous_local_cap",
            rule="local_model_cap",
            dropped=False,
        )
    _local_model_calls_this_pass += 1
    try:
        out = _local_model_fn(candidate)
    except Exception:
        return Judgment(tier=TIER_2, why="ambiguous_local_error", rule="local_model_error")
    if out is None:
        return None
    if not isinstance(out, Judgment):
        return Judgment(tier=TIER_2, why="ambiguous_local_bad_shape", rule="local_model")
    if not out.why:
        return Judgment(tier=out.tier, why="local_model_no_why", rule=out.rule or "local_model", dropped=out.dropped)
    return out


def classify(candidate: dict[str, Any], *, cloud_enabled: bool = False) -> Judgment:
    """Classify one mail/chat candidate. Rules first; local model for middle; cloud off.

    ``cloud_enabled`` is accepted for the explicit switch path but Path A default
    keeps it off. Ambiguous middle prefers the local-model hook when set (AC-P7).
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

    # Ambiguous middle: local model first (AC-P7).
    local = _try_local_model(candidate)
    if local is not None:
        return local

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
