"""Probe round-3 execution defects for work_watch Path A.

Round-2 left three holes the 40 green tests did not cover:
1. spawn_context_thread returns an UNSTARTED thread — callers must .start()
2. scheduler wakes every 60s and Graph-polls every wake (must be 10 min / hourly)
3. production path never installs the local-model middle (ambiguous → Tier-3 drop)

These tests fail on 65ab9505095 and must stay green after the r3 fix.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _ensure_pkg():
    root = str(REPO)
    if root not in sys.path:
        sys.path.insert(0, root)
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO / "plugins")]
        sys.modules["plugins"] = pkg
    for name in (
        "plugins.work_watch.auth",
        "plugins.work_watch.reader",
        "plugins.work_watch.service",
        "plugins.work_watch.judge",
        "plugins.work_watch",
    ):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    return (
        importlib.import_module("plugins.work_watch.auth"),
        importlib.import_module("plugins.work_watch.store"),
        importlib.import_module("plugins.work_watch.judge"),
        importlib.import_module("plugins.work_watch.reader"),
        importlib.import_module("plugins.work_watch.service"),
        importlib.import_module("plugins.work_watch"),
    )


@pytest.fixture()
def ww(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    for k in (
        "GRAPH_WATCH_REFRESH_TOKEN",
        "GRAPH_WATCH_CLIENT_ID",
        "GRAPH_WATCH_TENANT",
        "GRAPH_WATCH_IDENTITY_MASKED",
        "GRAPH_WATCH_CLOUD_JUDGE",
    ):
        monkeypatch.delenv(k, raising=False)

    auth, store, judge, reader, service, plugin = _ensure_pkg()
    auth.set_access_token_memory(None)
    auth.cancel_device_flow()
    service.clear_reader_stop()
    service.stop_poll_scheduler()
    judge.set_local_model_fn(None)
    judge.reset_local_model_budget()

    env_file = hermes / ".env"
    env_file.write_text("", encoding="utf-8")

    def _save(key, value):
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        out = [ln for ln in lines if not ln.startswith(f"{key}=")]
        out.append(f"{key}={value}")
        env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.environ[key] = value

    def _remove(key):
        if not env_file.exists():
            os.environ.pop(key, None)
            return False
        lines = env_file.read_text(encoding="utf-8").splitlines()
        new = [ln for ln in lines if not ln.startswith(f"{key}=")]
        found = len(new) < len(lines)
        env_file.write_text("\n".join(new) + ("\n" if new else ""), encoding="utf-8")
        os.environ.pop(key, None)
        return found

    monkeypatch.setattr("hermes_cli.config.save_env_value", _save)
    monkeypatch.setattr("hermes_cli.config.remove_env_value", _remove)

    db = tmp_path / "ww.db"
    yield types.SimpleNamespace(
        auth=auth,
        store=store,
        judge=judge,
        reader=reader,
        service=service,
        plugin=plugin,
        db=str(db),
        hermes=hermes,
        env_file=env_file,
        monkeypatch=monkeypatch,
    )
    service.stop_poll_scheduler()
    judge.set_local_model_fn(None)
    auth.cancel_device_flow()


# ---------------------------------------------------------------------------
# Defect 1 — real spawn_context_thread must be .start()'d (AC-P1 / AC-N6)
# ---------------------------------------------------------------------------


class TestR3_SpawnStarts:
    def test_device_poller_starts_real_spawn_context_thread(self, ww):
        """Production spawn_context_thread returns unstarted; we must call .start()."""
        ww.monkeypatch.setenv("GRAPH_WATCH_CLIENT_ID", "cid")
        started = {"n": 0}
        ran = {"n": 0}

        class _Resp:
            status_code = 200
            content = b"{}"

            def json(self):
                return {
                    "user_code": "U1",
                    "device_code": "D1",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 60,
                    "interval": 30,
                }

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        def real_shape_spawn(target, name=None, daemon=True, args=(), kwargs=None):
            """Match agent.memory_provider.spawn_context_thread: unstarted Thread."""
            def wrapper(*a, **k):
                ran["n"] += 1

            t = threading.Thread(target=wrapper, name=name or "t", daemon=daemon)
            # Capture start so we know production called it.
            orig_start = t.start

            def counting_start():
                started["n"] += 1
                return orig_start()

            t.start = counting_start  # type: ignore[method-assign]
            return t

        mp = types.ModuleType("agent.memory_provider")
        mp.spawn_context_thread = real_shape_spawn
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.memory_provider"] = mp

        ww.auth.set_httpx_client_factory(lambda: _Client())
        # Prevent the real poll body from hanging if it somehow runs with real httpx
        ww.auth._poller_stop.set()
        out = ww.auth.start_device_code_sign_in()
        # Give the started thread a tick
        time.sleep(0.05)
        thr = ww.auth._poller_thread
        ww.auth.cancel_device_flow()
        ww.auth.set_httpx_client_factory(None)

        assert out["ok"] is True
        assert thr is not None
        assert started["n"] == 1, "spawn_context_thread result must be .start()'d"
        # Bare-thread fallback must not be the happy path when spawn exists
        assert thr.name == "work-watch-device-code"

    def test_poll_scheduler_starts_real_spawn_context_thread(self, ww):
        started = {"n": 0}
        ran = threading.Event()

        def real_shape_spawn(target, name=None, daemon=True, args=(), kwargs=None):
            def wrapper(*a, **k):
                ran.set()
                # Exit quickly once woken once
                try:
                    target(*a, **(kwargs or {}))
                except Exception:
                    pass

            t = threading.Thread(target=wrapper, name=name or "t", daemon=daemon)
            orig_start = t.start

            def counting_start():
                started["n"] += 1
                return orig_start()

            t.start = counting_start  # type: ignore[method-assign]
            return t

        mp = types.ModuleType("agent.memory_provider")
        mp.spawn_context_thread = real_shape_spawn
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.memory_provider"] = mp

        # First sleep: stop the loop so the thread exits
        ww.service.set_sleep_fn(lambda s: ww.service._scheduler_stop.set())
        ok = ww.service.start_poll_scheduler(db_path=ww.db)
        assert ok is True
        # Wait briefly for start + one tick
        deadline = time.time() + 2.0
        while time.time() < deadline and started["n"] < 1:
            time.sleep(0.02)
        alive_soon = ww.service.scheduler_running() or ran.is_set() or started["n"] == 1
        ww.service.stop_poll_scheduler()
        ww.service.set_sleep_fn(None)

        assert started["n"] == 1, "poll scheduler must .start() the spawn_context_thread"
        assert alive_soon, "scheduler thread must actually run after start"


# ---------------------------------------------------------------------------
# Defect 2 — Graph poll cadence 10 min active / 60 min off-hours (AC-P4)
# ---------------------------------------------------------------------------


class TestR3_PollCadence:
    def test_scheduler_does_not_graph_poll_on_sub_interval_wakes(self, ww):
        """Short wakes (digest) must not call run_once / Graph under the poll interval."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ct = ZoneInfo("America/Chicago")
        # Active hours → 10 min interval
        now0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=ct)
        interval_min = ww.service.poll_interval_minutes(now0)
        assert interval_min == 10

        sleeps: list[float] = []
        run_calls = {"n": 0}
        tick = {"i": 0}
        clock = {"t": now0}

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            tick["i"] += 1
            # Advance wall by the slept amount (cap test timeline)
            clock["t"] = clock["t"] + __import__("datetime").timedelta(seconds=min(s, 60.0))
            if tick["i"] >= 5:
                ww.service._scheduler_stop.set()

        def fake_run_once(**kwargs):
            run_calls["n"] += 1
            return {"ok": True}

        ww.monkeypatch.setattr(ww.service, "run_once", fake_run_once)
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "rt-test")
        ww.service.clear_reader_stop()
        ww.service.reset_scheduler_poll_clock()

        def fake_now(tz=None):
            return clock["t"]

        ww.service.set_sleep_fn(fake_sleep)
        ww.service.set_now_fn(fake_now)
        ww.service._scheduler_stop.clear()
        ww.service._scheduler_loop(db_path=ww.db)
        ww.service.set_sleep_fn(None)
        ww.service.set_now_fn(None)

        # Under ~5 minutes of wall, at most ONE Graph poll is allowed (first/prime).
        assert run_calls["n"] <= 1, (
            f"Graph polled {run_calls['n']} times across sub-interval wakes; "
            f"must honor {interval_min} min cadence (got sleeps={sleeps})"
        )

    def test_graph_poll_fires_after_full_interval(self, ww):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        ct = ZoneInfo("America/Chicago")
        run_calls = {"n": 0}
        tick = {"i": 0}
        clock = {"t": datetime(2026, 10, 30, 10, 0, 0, tzinfo=ct)}

        def fake_sleep(s: float) -> None:
            tick["i"] += 1
            # First wake: small settle; second: jump a full 10 min so Graph is due again
            if tick["i"] == 1:
                clock["t"] = clock["t"] + timedelta(seconds=5)
            elif tick["i"] == 2:
                clock["t"] = clock["t"] + timedelta(minutes=10)
            else:
                clock["t"] = clock["t"] + timedelta(seconds=5)
            if tick["i"] >= 3:
                ww.service._scheduler_stop.set()

        def fake_run_once(**kwargs):
            run_calls["n"] += 1
            return {"ok": True}

        ww.monkeypatch.setattr(ww.service, "run_once", fake_run_once)
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "rt-test")
        ww.service.clear_reader_stop()
        ww.service.reset_scheduler_poll_clock()

        def fake_now(tz=None):
            return clock["t"]

        ww.service.set_sleep_fn(fake_sleep)
        ww.service.set_now_fn(fake_now)
        ww.service._scheduler_stop.clear()
        ww.service._scheduler_loop(db_path=ww.db)
        ww.service.set_sleep_fn(None)
        ww.service.set_now_fn(None)

        assert run_calls["n"] >= 2, (
            f"expected Graph poll on first eligible + after full interval, got {run_calls['n']}"
        )


# ---------------------------------------------------------------------------
# Defect 3 — production local-model middle (AC-P7); no silent Tier-3 drop
# ---------------------------------------------------------------------------


class TestR3_LocalModelProduction:
    def test_production_hook_does_not_drop_ambiguous_as_no_action_signal(self, ww):
        """After production wiring, ambiguous mail is model-classified or model_unavailable —
        never Tier-3 no_action_signal."""
        # Clear any test injection, then install production default
        ww.judge.set_local_model_fn(None)
        assert hasattr(ww.plugin, "install_production_judge") or hasattr(
            ww.judge, "install_production_local_model"
        ), "production must expose a way to wire the local-model middle"
        if hasattr(ww.plugin, "install_production_judge"):
            ww.plugin.install_production_judge()
        else:
            ww.judge.install_production_local_model()

        j = ww.judge.classify(
            {
                "subject": "Quarterly numbers",
                "sender": "peer@contoso.com",
                "preview": "attached spreadsheet for review when free",
                "conversation_id": "amb-prod-1",
            }
        )
        assert j.why != "no_action_signal", (
            f"ambiguous production path dropped as Tier-3 no_action_signal (rule={j.rule})"
        )
        assert not (j.dropped and j.tier == 3 and j.why == "no_action_signal")
        # Either a real local judgment or an honest model_unavailable (still not dropped as noise)
        assert j.why in {
            "model_unavailable",
            "ambiguous_local_error",
            "ambiguous_local_cap",
            "ambiguous_local_bad_shape",
        } or j.why.startswith("local:") or j.rule in {
            "local_model",
            "local_model_unavailable",
            "local_model_error",
            "local_model_cap",
        }
        if j.why == "model_unavailable" or j.rule == "local_model_unavailable":
            assert j.dropped is False
            assert j.tier == 2

    def test_register_installs_production_local_model(self, ww):
        """register() must wire the middle so live gateway path is not bare."""
        ww.judge.set_local_model_fn(None)

        class _Ctx:
            def register_tool(self, **kwargs):
                pass

            def register_command(self, **kwargs):
                pass

            def register_cli_command(self, **kwargs):
                pass

        # Avoid starting a real long-lived scheduler in the test process
        ww.monkeypatch.setattr(ww.service, "start_poll_scheduler", lambda **k: False)
        ww.plugin.register(_Ctx())

        j = ww.judge.classify(
            {
                "subject": "Team sync notes draft",
                "sender": "alex@contoso.com",
                "preview": "draft for tomorrow",
                "conversation_id": "amb-reg-1",
            }
        )
        assert j.why != "no_action_signal"
