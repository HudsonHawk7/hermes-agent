"""work_watch plugin — Path A read-only mail + Teams watcher."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register tools + slash command. Dashboard API loads via dashboard/manifest."""
    from plugins.work_watch import service
    from plugins.work_watch.constants import PLUGIN_LABEL, REFRESH_TOKEN_ENV

    def _status_tool(args=None, **kwargs):
        snap = service.settings_snapshot()
        # Never include tokens
        return json.dumps(snap, indent=2)

    def _poll_tool(args=None, **kwargs):
        return json.dumps(service.run_once(), indent=2)

    def _revoke_tool(args=None, **kwargs):
        from plugins.work_watch import auth

        return json.dumps(auth.revoke(), indent=2)

    def _flags_tool(args=None, **kwargs):
        from plugins.work_watch import store

        with store.open_store() as conn:
            payload = {
                "summary": store.board_summary(conn),
                "flags": store.list_flags(conn, include_local_content=False),
            }
        return json.dumps(payload, indent=2)

    status_schema = {
        "name": "work_watch_status",
        "description": (
            f"Show {PLUGIN_LABEL} status (Off/Signed in/etc). "
            f"Never returns tokens. Setting name: {REFRESH_TOKEN_ENV}."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }
    poll_schema = {
        "name": "work_watch_poll",
        "description": "Run one Work watch poll pass (local judge). No-op Graph when unsigned.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }
    revoke_schema = {
        "name": "work_watch_revoke",
        "description": "Revoke local Graph refresh token and stop the reader.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }
    flags_schema = {
        "name": "work_watch_flags",
        "description": "List Work watch flags as counts + why tags (no message bodies).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }

    for name, schema, handler, emoji in (
        ("work_watch_status", status_schema, _status_tool, "📬"),
        ("work_watch_poll", poll_schema, _poll_tool, "🔄"),
        ("work_watch_revoke", revoke_schema, _revoke_tool, "🔓"),
        ("work_watch_flags", flags_schema, _flags_tool, "🏁"),
    ):
        ctx.register_tool(
            name=name,
            toolset="work_watch",
            schema=schema,
            handler=handler,
            emoji=emoji,
        )

    def _cmd(raw_args: str = "") -> str:
        arg = (raw_args or "").strip().lower()
        if arg in {"", "status"}:
            return _status_tool()
        if arg in {"poll", "run"}:
            return _poll_tool()
        if arg == "revoke":
            return _revoke_tool()
        if arg == "flags":
            return _flags_tool()
        if arg in {"sign-in", "signin", "login"}:
            from plugins.work_watch import auth

            return json.dumps(auth.start_device_code_sign_in(), indent=2)
        return "Usage: /work-watch [status|poll|flags|revoke|sign-in]"

    ctx.register_command(
        name="work-watch",
        handler=_cmd,
        description="Work mail + Teams watcher status / poll / revoke / sign-in",
        args_hint="[status|poll|flags|revoke|sign-in]",
    )

    def _cli_setup(parser):
        sub = parser.add_subparsers(dest="work_watch_cmd")
        sub.add_parser("status", help="Show status")
        sub.add_parser("poll", help="One poll pass")
        sub.add_parser("flags", help="List flags (no bodies)")
        sub.add_parser("revoke", help="Revoke refresh token")
        sub.add_parser("sign-in", help="Start device-code sign-in")

    def _cli_handler(args):
        cmd = getattr(args, "work_watch_cmd", None) or "status"
        print(_cmd(cmd.replace("_", "-")))
        return 0

    ctx.register_cli_command(
        name="work-watch",
        help="Work mail + Teams watcher",
        setup_fn=_cli_setup,
        handler_fn=_cli_handler,
        description="Path A delegated Graph watcher (local judge, Work watch page).",
    )
