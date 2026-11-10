"""Parser for ``hermes force-live-update``."""

from __future__ import annotations


def build_force_live_update_parser(subparsers, *, cmd_force_live_update, **_ignored):
    parser = subparsers.add_parser(
        "force-live-update",
        help="Run the HermesForce live-update protocol (quiet window, ff, restart, proof, rollback)",
        description=(
            "One command for protocol steps 2-9. It changes the live install. "
            "Only Architect_ClaudeCode runs it, after a JocastaNu PASS. "
            "A kanban worker must pass --allow-live."
        ),
    )
    parser.add_argument("--to", required=True, help="Merged SHA on fork/force/carry to land")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Pack Desktop into a staging folder, check install-stamp.json, and swap",
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Required when a kanban worker targets the live install (Architect_ClaudeCode only)",
    )
    parser.set_defaults(func=cmd_force_live_update)
    return parser
