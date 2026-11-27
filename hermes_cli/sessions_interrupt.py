"""``hermes sessions interrupt``: stop a running turn the same way the Desktop stop button does.

``session.resume`` then ``session.interrupt`` with that turn's ``execution_generation``.
It does not start a new turn. An idle session is a no-op.
"""
from __future__ import annotations

import sys


async def _resume(client, name):
    """Exact session id first, then title, as ``hermes chat --resume`` resolves it."""
    from hermes_cli.gateway_client import GatewayClientError
    for key in ("session_id", "title"):
        try:
            return await client.rpc("session.resume", **{key: name})
        except GatewayClientError as exc:
            if str(exc) != "not_found":
                raise
    raise GatewayClientError(
        f"No session found matching '{name}'. Use 'hermes sessions list' to see available sessions.")


async def _interrupt(args):
    from hermes_cli.gateway_client import connect_gateway
    async with connect_gateway() as client:
        snapshot = await _resume(client, args.session)
        session_id = snapshot.get("session_id") or snapshot["stored_session_id"]
        if not snapshot.get("running"):
            print(f"Session {session_id} is not running.")
            return 0
        generation = snapshot.get("execution_generation")
        await client.rpc("session.interrupt", session_id=session_id, execution_generation=generation)
        print(f"Interrupted session {session_id} (generation {generation}).")
        return 0


def add_interrupt_parser(sessions_subparsers) -> None:
    parser = sessions_subparsers.add_parser(
        "interrupt",
        help="Stop a running turn (same path as the Desktop stop button)",
        description="Stop the running turn on a session. This is the script form of the Desktop "
            "stop button: it sends session.interrupt with the generation the gateway is running. "
            "It does not start a new turn. An idle session is left alone. "
            "Usage: hermes sessions interrupt <session-id>  "
            "(hermes -p <profile> sessions interrupt <session-id> for another profile).")
    parser.add_argument("session", help="Session ID or title")


def cmd_interrupt(args) -> int:
    import asyncio
    from websockets.exceptions import WebSocketException
    from hermes_cli.gateway_client import GatewayClientError
    try:
        return asyncio.run(_interrupt(args))
    except (GatewayClientError, OSError, TimeoutError, WebSocketException) as exc:
        message = str(exc) if isinstance(exc, GatewayClientError) else "Gateway connection/read failed"
        print("Error: " + message, file=sys.stderr)
        return 1
