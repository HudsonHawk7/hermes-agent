#!/usr/bin/env python3
"""Seed two boards of GM asks into a scratch HERMES_HOME for desktop e2e."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, help="HERMES_HOME path")
    parser.add_argument("--out", required=True, help="JSON path for seeded task ids")
    args = parser.parse_args()

    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    # Clear live-board pins and the delegate_task fence so seeding a scratch
    # HERMES_HOME works even when this process inherits a kanban worker env.
    for key in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_SESSION_SOURCE",
    ):
        os.environ.pop(key, None)

    # Repo root is four levels up from this file: e2e → desktop → apps → root
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_boards as boards
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_gm_asks as gm

    kb.init_db()
    boards.create_board("alpha", name="Alpha")
    boards.create_board("beta", name="Beta")

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    def running(conn, title: str, assignee: str = "architect") -> str:
        tid = kb.create_task(conn, title=title, assignee=assignee)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        claimed = kb.claim_task(conn, tid, claimer=assignee)
        assert claimed is not None
        return tid

    ids: dict[str, str] = {}

    with kbc.connect_closing(board="alpha") as conn:
        yes_id = running(conn, "Rebase Alpha PR?")
        gm.record_gm_ask(
            conn,
            yes_id,
            {
                "question": "Rebase onto develop?",
                "context": "PR is behind mainline.",
                "options": [
                    {"key": "yes", "label": "Yes", "effect": "BB-8 rebases it"},
                    {"key": "no", "label": "No", "effect": "close the card"},
                ],
                "pick": "yes",
            },
            asked_by="architect",
            reason="need GM call",
        )
        ids["yes"] = yes_id

        ab_id = running(conn, "Pick mockup A or B")
        att_a = kb.store_attachment_bytes(
            conn,
            ab_id,
            "a.png",
            png,
            content_type="image/png",
            uploaded_by="architect",
        )
        att_b = kb.store_attachment_bytes(
            conn,
            ab_id,
            "b.png",
            png,
            content_type="image/png",
            uploaded_by="architect",
        )
        # Option image is the attachment filename (or path leaf) — resolved to
        # image_url by list_needs_you's attachment_url hook.
        del att_a, att_b
        gm.record_gm_ask(
            conn,
            ab_id,
            {
                "question": "Which mockup?",
                "context": "A is denser; B is airier.",
                "options": [
                    {"key": "a", "label": "A", "effect": "ship dense layout", "image": "a.png"},
                    {"key": "b", "label": "B", "effect": "ship airy layout", "image": "b.png"},
                ],
                "pick": "a",
            },
            asked_by="hera",
        )
        ids["ab"] = ab_id

        note_id = running(conn, "Need more context")
        gm.record_gm_ask(
            conn,
            note_id,
            {
                "question": "Ship tonight?",
                "context": "Risk is medium.",
                "options": [
                    {"key": "ship", "label": "Ship", "effect": "lands tonight"},
                    {"key": "hold", "label": "Hold", "effect": "wait for QA"},
                ],
                "pick": "hold",
            },
            asked_by="probe",
        )
        ids["note"] = note_id

        undo_id = running(conn, "Undo me")
        gm.record_gm_ask(
            conn,
            undo_id,
            {
                "question": "Keep the flag?",
                "context": "Feature flag for Needs you.",
                "options": [
                    {"key": "keep", "label": "Keep", "effect": "flag stays"},
                    {"key": "drop", "label": "Drop", "effect": "flag goes"},
                ],
                "pick": "keep",
            },
            asked_by="architect",
        )
        ids["undo"] = undo_id

        # Legacy needs_input with no structured ask (free-text reason only).
        legacy_id = kb.create_task(conn, title="Legacy free-text block", assignee="worker")
        assert kb.block_task(
            conn,
            legacy_id,
            reason="Which branch should this land on?",
            kind="needs_input",
        )
        ids["legacy"] = legacy_id

        fail_id = running(conn, "Fail path card")
        gm.record_gm_ask(
            conn,
            fail_id,
            {
                "question": "Force a fail?",
                "context": "Used by the e2e fail-POST path via page route.",
                "options": [
                    {"key": "yes", "label": "Yes", "effect": "should roll back"},
                    {"key": "no", "label": "No", "effect": "stay"},
                ],
                "pick": "no",
            },
            asked_by="hera",
        )
        ids["fail"] = fail_id

        # Cliff Notes: 4-option grid + three facts + pick (Recommend line).
        cliff_id = running(conn, "Cliff notes four-option")
        gm.record_gm_ask(
            conn,
            cliff_id,
            {
                "question": "Ship cliff notes layout?",
                "context": "Mockups are the contract. Details start closed.",
                "facts": [
                    "Question + recommend + ≤3 facts above the fold",
                    "Comments newest-first; ×3+ same-author fold",
                    "hermes kanban ask --fact wires write-time caps",
                ],
                "options": [
                    {"key": "a", "label": "A", "effect": "two-column first"},
                    {"key": "b", "label": "B", "effect": "two-column second"},
                    {"key": "c", "label": "C", "effect": "two-column third"},
                    {"key": "d", "label": "D", "effect": "two-column fourth"},
                ],
                "pick": "a",
            },
            asked_by="hera",
        )
        ids["cliff"] = cliff_id

        # Folded repeat: 12 identical worker pings + 13 unique → 14 drawer rows.
        fold_id = running(conn, "Folded repeat comments")
        gm.record_gm_ask(
            conn,
            fold_id,
            {
                "question": "Open the fold?",
                "context": "Twelve identical pings should collapse.",
                "facts": ["Fold needs three or more identical bodies"],
                "options": [
                    {"key": "yes", "label": "Yes", "effect": "expand stamps"},
                    {"key": "no", "label": "No", "effect": "leave folded"},
                ],
                "pick": "yes",
            },
            asked_by="hera",
        )
        base_ts = int(__import__("time").time())
        # Interleave 12 identical worker pings with 13 unique notes so a
        # consecutive-only fold cannot green-fake (design note v4 §4).
        # Newest-first: ping, unique, ping, unique… → one global ×12 fold.
        for i in range(12):
            cid = kb.add_comment(conn, fold_id, "worker", "ping status")
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_comments SET created_at=? WHERE id=?",
                    (base_ts - i * 120, cid),
                )
        for i in range(13):
            cid = kb.add_comment(conn, fold_id, "architect", f"unique note {i}")
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_comments SET created_at=? WHERE id=?",
                    (base_ts - 60 - i * 120, cid),
                )
        ids["fold"] = fold_id

    with kbc.connect_closing(board="beta") as conn:
        beta_id = running(conn, "Beta board ask")
        gm.record_gm_ask(
            conn,
            beta_id,
            {
                "question": "Promote beta?",
                "context": "Cross-board count check.",
                "options": [
                    {"key": "yes", "label": "Yes", "effect": "promotes"},
                    {"key": "no", "label": "No", "effect": "stays"},
                ],
                "pick": "yes",
            },
            asked_by="voss",
        )
        ids["beta"] = beta_id

    Path(args.out).write_text(json.dumps({"board_alpha": "alpha", "board_beta": "beta", "ids": ids}, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "ids": ids}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
