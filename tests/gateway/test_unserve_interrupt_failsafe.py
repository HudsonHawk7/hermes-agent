"""Unserve must stop a no-arg interrupt ABI and never leave a half-retired profile."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from gateway import run_runtime
from gateway.run_profile_reconcile import (
    GatewayProfileReconcileMixin,
    unserve_profile_verb,
)
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_authorities import SessionAuthorities
from gateway.session_managed_worker import ManagedWorker
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


def _served_authority(tmp_path):
    launch = tmp_path / "default"
    served = tmp_path / "hera"
    launch.mkdir()
    served.mkdir()
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", source="test")
    epoch = begin_runtime_epoch(db, instance_id="owner")
    runner = SimpleNamespace(
        _draining=False,
        config=SimpleNamespace(multiplex_profiles=True),
        _adapter_for_source=lambda source: None,
        _running_agents={},
        hosted_room_service=None,
        session_ticket_store=SimpleNamespace(profile_ids=frozenset()),
        session_runtime_descriptor={},
        _persist_active_agents=None,
    )
    profile_id = str(served.resolve())
    authority = SessionAuthority(
        runner, profile_id=profile_id, instance_id="owner", db=db, epoch=epoch
    )
    launch_id = str(launch.resolve())
    registry = SessionAuthorities(launch, multiplexed=True)
    registry.add(
        launch,
        SimpleNamespace(profile_id=launch_id, retiring=False, retired=False),
        name=None,
    )
    registry.add(served, authority, name="hera")
    runner.session_authorities = registry
    runner.session_authority = authority
    runner.session_ticket_store.profile_ids = frozenset(
        {launch_id, profile_id}
    )
    return db, runner, authority, served


@pytest.mark.asyncio
async def test_unserve_stops_managed_worker_abi_running_agent(
    tmp_path, monkeypatch
):
    db, runner, authority, served = _served_authority(tmp_path)
    frames = []
    worker = ManagedWorker.__new__(ManagedWorker)
    worker.control = frames.append
    live = LiveSession(SimpleNamespace(platform=None, user_id="human"), "route")
    live.event_stream.execution["execution_generation"] = 1
    authority.sessions["s"] = live
    runner._running_agents["route"] = worker
    monkeypatch.setattr(
        "gateway.session_cron.unbind_owner", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "gateway.session_llm_oneshot.stop", lambda *_a, **_k: None
    )
    try:
        retired = await run_runtime.unserve_profile_runtime(runner, served)
        assert retired is True
        assert frames
        assert all(frame == {"type": "stop"} for frame in frames)
        assert runner.session_authorities.for_home(served) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_raising_retire_keeps_authority_served(tmp_path, monkeypatch):
    db, runner, authority, served = _served_authority(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("stop failed")

    monkeypatch.setattr(
        "gateway.session_runtime_workers.stop_authority_work", _boom
    )
    try:
        with pytest.raises(RuntimeError, match="stop failed"):
            await run_runtime._retire_profile_authority(authority)
        assert authority.retiring is False
        assert authority.retired is False
        assert runner.session_authorities.for_home(served) is authority
        served_ids = {
            p["profile_id"]
            for p in runner.session_runtime_descriptor["served_profiles"]
        }
        assert authority.profile_id in served_ids
        assert authority.profile_id in runner.session_ticket_store.profile_ids
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unserve_verb_restores_home_when_unserve_pops_then_raises(
    tmp_path, monkeypatch
):
    home = tmp_path / "hera"
    home.mkdir()

    class _Runner(GatewayProfileReconcileMixin):
        def __init__(self):
            self._running = True
            self._served_profile_homes = {"hera": home}
            self._served_profile_signatures = {}
            self._profile_adapters = {}
            self._primary_profile_name = "default"

        def _multiplex_on(self):
            return True

        def _record_served_profiles(self, active, homes):
            raise AssertionError("must not record a failed unserve")

    @contextmanager
    def _noop(*_a, **_k):
        yield

    monkeypatch.setattr("gateway.run._profile_runtime_scope", _noop)
    monkeypatch.setattr(
        "gateway.run._write_runtime_status_quiet", lambda **_k: None
    )

    def _boom(*_a, **_k):
        raise RuntimeError("release failed")

    monkeypatch.setattr("gateway.run_runtime.release_profile_home", _boom)

    runner = _Runner()
    runner.unserve_profile_verb = unserve_profile_verb(runner)
    result = await asyncio.to_thread(
        runner.unserve_profile_verb, {"name": "hera"}
    )
    assert "error" in result
    assert "hera" in runner._served_profile_homes
    assert runner._served_profile_homes["hera"] == home
