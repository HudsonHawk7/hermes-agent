"""``hermes sessions interrupt`` sends the same ``session.interrupt`` the Desktop stop button sends."""
import argparse
import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_sessions_interrupt_stops_the_running_turn_with_its_generation(monkeypatch, capsys):
    """Resume by id, then session.interrupt with the generation the owner is running. An idle session is left alone."""
    from websockets.asyncio.server import serve

    from hermes_cli import sessions_cmd
    from hermes_cli.subcommands.sessions import build_sessions_parser

    snapshot = {
        "session_id": "local-abc",
        "stored_session_id": "local-abc",
        "running": True,
        "execution_generation": 7,
    }
    calls = []

    async def peer(ws):
        async for raw in ws:
            request = json.loads(raw)
            calls.append((request["method"], request["params"]))
            if request["method"] == "session.resume" and "session_id" in request["params"]:
                reply = {"error": {"code": 4001, "message": "not_found", "data": {"reason": "not_found"}}}
            else:
                reply = {"result": snapshot if request["method"] == "session.resume" else {"interrupted": True}}
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], **reply}))

    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=sessions_cmd.cmd_sessions)
    async with serve(peer, "127.0.0.1", 0) as server:
        monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")

        def run():
            args = parser.parse_args(["sessions", "interrupt", "my script session"])
            return args.func(args)

        assert await asyncio.to_thread(run) == 0

    assert calls == [
        ("session.resume", {"session_id": "my script session"}),
        ("session.resume", {"title": "my script session"}),
        ("session.interrupt", {"session_id": "local-abc", "execution_generation": 7}),
    ]
    assert capsys.readouterr().out.strip() == "Interrupted session local-abc (generation 7)."


@pytest.mark.asyncio
async def test_sessions_interrupt_leaves_an_idle_session_alone(monkeypatch, capsys):
    from websockets.asyncio.server import serve

    from hermes_cli import sessions_cmd
    from hermes_cli.subcommands.sessions import build_sessions_parser

    snapshot = {
        "session_id": "local-idle",
        "stored_session_id": "local-idle",
        "running": False,
        "execution_generation": None,
    }
    calls = []

    async def peer(ws):
        async for raw in ws:
            request = json.loads(raw)
            calls.append(request["method"])
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": request["id"], "result": snapshot}))

    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=sessions_cmd.cmd_sessions)
    async with serve(peer, "127.0.0.1", 0) as server:
        monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")

        def run():
            args = parser.parse_args(["sessions", "interrupt", "local-idle"])
            return args.func(args)

        assert await asyncio.to_thread(run) == 0

    assert calls == ["session.resume"]
    assert "not running" in capsys.readouterr().out
