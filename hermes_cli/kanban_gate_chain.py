"""File a UI gate chain that can finish in order.

Probe posts force/ui-proof. Critiquo posts force/ui-verified, and cannot
start until that proof is done. JocastaNu posts force/code-review and is
the only card that waits on every repository-required check plus a merge.
A card never parents the cards that produce the statuses it waits on.
"""
from __future__ import annotations

from hermes_cli.kanban_db import create_task

PROOF = "force/ui-proof"
VERIFIED = "force/ui-verified"
REVIEW = "force/code-review"


def file_ui_gate_chain(conn, *, pr_url: str, title: str) -> dict[str, str]:
    """Create probe → critiquo → jocastanu. Returns those card ids."""
    url = pr_url.split("#", 1)[0]
    probe = create_task(
        conn,
        title=f"UI proof: {title}",
        assignee="probe",
        body=f"Post {PROOF} on {url}. This card owns that check only.",
        completion_contract=f"{url}#{PROOF}",
    )
    crit = create_task(
        conn,
        title=f"UI verify: {title}",
        assignee="critiquo",
        parents=[probe],
        body=f"Post {VERIFIED} on {url} after the proof card is done.",
        completion_contract=f"{url}#{VERIFIED}",
    )
    review = create_task(
        conn,
        title=f"Code review and merge: {title}",
        assignee="jocastanu",
        parents=[crit],
        body=(
            f"Post {REVIEW} on {url}, then merge. This card owns every "
            "repository-required check plus the merge."
        ),
        completion_contract=f"{url}#all,{REVIEW},merged",
    )
    return {"probe": probe, "critiquo": crit, "jocastanu": review}
