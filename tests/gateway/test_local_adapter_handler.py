"""A recovered Local adapter must already have the gateway message handler.

Platform connect wires Telegram and the rest. A Local adapter is built by
session recovery, not by connect, so it used to have no handler. Every inbound
wake into that chat was then dropped once per process
(``[Local] Dropping inbound message: no gateway message handler is installed``).
"""

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


def _close(runners):
    for runner in runners:
        for owner in getattr(runner, "session_authorities", ()):
            owner.db.close()


async def _restored(tmp_path, monkeypatch, which):
    from gateway import run
    from gateway.run import _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import restore_local_session

    monkeypatch.setattr(
        run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}}
    )
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=("alpha",))
    process_ownership.reserve([home for _, home in homes])
    name, home = homes[0] if which == "launch" else homes[1]
    first = _runner(root, homes)
    runners = [first]
    try:
        await initialize_gateway_runtime(first)
        authority = first.session_authorities.for_home(home)
        actor = Principal(
            "owner",
            authority.profile_id,
            frozenset({"session:create", "session:read", "session:control"}),
            "socket",
        )
        with _profile_runtime_scope(home, {}):
            ref = create_local_session(
                authority,
                actor,
                {
                    "request_id": "desktop",
                    "source": "gui",
                    "cwd": str(tmp_path),
                    "model": "frozen",
                    "toolsets": [],
                },
            )
        for owner in first.session_authorities:
            owner.db.close()
        second = _runner(root, homes)
        runners.append(second)
        await initialize_gateway_runtime(second)
        restored = second.session_authorities.for_home(home)
        with _profile_runtime_scope(home, {}):
            assert restore_local_session(restored, ref.session_id) == ref
        return second, name, homes, runners
    except Exception:
        _close(runners)
        for _, owned in homes:
            process_ownership.release(owned)
        raise


@pytest.mark.asyncio
async def test_secondary_local_adapter_is_wired_on_restore(tmp_path, monkeypatch):
    """A named profile (grandmaster's case) must not restore a handler-less Local adapter."""
    from gateway.platforms.base import Platform
    from gateway.runtime_ownership import process_ownership

    second, name, homes, runners = await _restored(tmp_path, monkeypatch, "secondary")
    try:
        adapter = second._profile_adapters[name][Platform.LOCAL]
        assert getattr(adapter, "_message_handler", None) is not None
        assert getattr(adapter, "_hermes_profile_name", None) == name
    finally:
        _close(runners)
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
async def test_launch_local_adapter_is_wired_on_restore(tmp_path, monkeypatch):
    """The launch profile's Local adapter lives on runner.adapters and needs the same handler."""
    from gateway.platforms.base import Platform
    from gateway.runtime_ownership import process_ownership

    second, name, homes, runners = await _restored(tmp_path, monkeypatch, "launch")
    try:
        adapter = second.adapters[Platform.LOCAL]
        assert getattr(adapter, "_message_handler", None) is not None
        assert Platform.LOCAL not in getattr(second, "_profile_adapters", {}).get(
            name, {}
        )
    finally:
        _close(runners)
        for _, home in homes:
            process_ownership.release(home)
