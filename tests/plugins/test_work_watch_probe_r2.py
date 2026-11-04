"""Probe round-2 defect coverage for work_watch Path A.

These tests fail on the pre-fix tree (msal-only, bare Thread, no scheduler,
no 401 retry, no digest/local-model, desktop title/icon). They must stay green
on force/carry after the six Probe fixes.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
import sys
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
    # Reload so carry fixes land even if an older import is cached.
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


# ---------------------------------------------------------------------------
# Defect 1 — device-code over httpx; msal never required (AC-P1)
# ---------------------------------------------------------------------------


class TestProbe1_HttpxNoMsal:
    def test_auth_module_does_not_import_msal(self, ww):
        src = inspect.getsource(ww.auth)
        assert "import msal" not in src
        assert "from msal" not in src
        # Must mention httpx device-code path
        assert "httpx" in src
        assert "urn:ietf:params:oauth:grant-type:device_code" in src

    def test_start_device_code_uses_httpx_not_msal_missing(self, ww):
        """With client id + fake httpx, start returns user_code — never MSAL_MISSING."""
        ww.monkeypatch.setenv("GRAPH_WATCH_CLIENT_ID", "public-client-id")

        class _Resp:
            status_code = 200
            content = b"{}"

            def json(self):
                return {
                    "user_code": "ABCD-EFGH",
                    "device_code": "dc-secret",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900,
                    "interval": 1,
                    "message": "Go to microsoft.com/devicelogin",
                }

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, data=None, headers=None):
                assert "devicecode" in url
                assert data["client_id"] == "public-client-id"
                assert "Mail.Read" in data["scope"]
                assert "client_secret" not in (data or {})
                return _Resp()

        ww.auth.set_httpx_client_factory(lambda: _Client())
        # Stop poller quickly
        ww.auth._poller_stop.set()
        out = ww.auth.start_device_code_sign_in()
        ww.auth.cancel_device_flow()
        ww.auth.set_httpx_client_factory(None)
        assert out["ok"] is True
        assert out.get("user_code") == "ABCD-EFGH"
        assert "MSAL_MISSING" not in str(out)
        assert "devicelogin" in (out.get("verification_uri") or "").lower()


# ---------------------------------------------------------------------------
# Defect 2 — device poller uses spawn_context_thread (AC-P1 / AC-N6)
# ---------------------------------------------------------------------------


class TestProbe2_SpawnContextThread:
    def test_start_device_code_uses_spawn_context_thread(self, ww):
        ww.monkeypatch.setenv("GRAPH_WATCH_CLIENT_ID", "cid")
        calls = {"n": 0}

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

        def fake_spawn(target, name=None, daemon=True):
            calls["n"] += 1
            calls["name"] = name
            t = types.SimpleNamespace(
                is_alive=lambda: False,
                join=lambda timeout=None: None,
                start=lambda: None,
                name=name or "t",
            )
            # Do not run target — we only care that spawn was chosen + started.
            return t

        # Inject fake agent.memory_provider
        mp = types.ModuleType("agent.memory_provider")
        mp.spawn_context_thread = fake_spawn
        agent_pkg = types.ModuleType("agent")
        sys.modules["agent"] = agent_pkg
        sys.modules["agent.memory_provider"] = mp

        ww.auth.set_httpx_client_factory(lambda: _Client())
        out = ww.auth.start_device_code_sign_in()
        ww.auth.cancel_device_flow()
        ww.auth.set_httpx_client_factory(None)
        assert out["ok"] is True
        assert calls["n"] == 1
        assert "device" in (calls.get("name") or "")


# ---------------------------------------------------------------------------
# Defect 3 — desktop half materialize + label/codicon (AC-P2 / AC-P6)
# ---------------------------------------------------------------------------


class TestProbe3_DesktopHalf:
    def test_plugin_js_uses_label_and_codicon(self):
        js = (REPO / "plugins" / "work_watch" / "desktop" / "plugin.js").read_text(encoding="utf-8")
        # Sidebar nav contribution (SDK: label + codicon, not title/icon)
        assert re.search(r"label:\s*'Work watch'", js)
        assert re.search(r"codicon:\s*'mail'", js)
        nav_block = js[js.find("work-watch-nav") : js.find("work-watch-nav") + 280]
        assert re.search(r"\btitle\s*:", nav_block) is None
        # bare icon: key (not codicon)
        assert re.search(r"(?<![a-z])icon\s*:", nav_block) is None

    def test_materialize_desktop_half_to_hermes_home(self, ww):
        dest = ww.plugin.materialize_desktop_half(hermes_home=str(ww.hermes))
        assert dest is not None
        path = Path(dest)
        assert path.is_file()
        assert "plugins" in path.parts
        assert "work_watch" in path.parts
        assert path.name == "plugin.js"
        text = path.read_text(encoding="utf-8")
        assert "label:" in text and "codicon:" in text


# ---------------------------------------------------------------------------
# Defect 4 — poll scheduler actually runs; chats discovered (AC-P4)
# ---------------------------------------------------------------------------


class TestProbe4_SchedulerAndChats:
    def test_start_poll_scheduler_spawns_thread(self, ww):
        calls = {"n": 0}

        def fake_spawn(target, name=None, daemon=True):
            calls["n"] += 1
            calls["name"] = name
            # Run one tick with immediate stop via fake sleep
            return types.SimpleNamespace(
                is_alive=lambda: True,
                join=lambda timeout=None: None,
                start=lambda: None,
                name=name or "t",
            )

        mp = types.ModuleType("agent.memory_provider")
        mp.spawn_context_thread = fake_spawn
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.memory_provider"] = mp

        # Prevent real loop from sleeping forever if spawn actually runs target
        ww.service.set_sleep_fn(lambda s: ww.service._scheduler_stop.set())
        started = ww.service.start_poll_scheduler(db_path=ww.db)
        assert started is True
        assert calls["n"] == 1
        assert "poll" in (calls.get("name") or "")
        ww.service.stop_poll_scheduler()
        ww.service.set_sleep_fn(None)

    def test_fetch_candidates_lists_chats(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "rt")
        hits = []

        def fake_get(url, headers, params):
            hits.append(url)
            if "messages/delta" in url or "mailFolders" in url:
                return {"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-next"}
            if url.rstrip("/").endswith("/me/chats"):
                return {
                    "value": [
                        {"id": "chat-1", "topic": "Release", "lastUpdatedDateTime": "2026-10-29T12:00:00Z"}
                    ]
                }
            if "/messages" in url:
                return {
                    "value": [
                        {
                            "id": "msg-1",
                            "from": {"user": {"displayName": "Pat"}},
                            "body": {"content": "FYI: heads-up on the cutover"},
                            "createdDateTime": "2026-10-29T12:01:00Z",
                        }
                    ]
                }
            return {"value": []}

        out = ww.reader.fetch_candidates(
            http_get=fake_get,
            token_provider=lambda: "access",
        )
        assert any("/me/chats" in u for u in hits)
        assert out["chats_discovered"] >= 1
        assert any(c.get("source") == "chat" for c in out["candidates"])


# ---------------------------------------------------------------------------
# Defect 5 — 401 → refresh once → retry; second 401 stops (AC-N9)
# ---------------------------------------------------------------------------


class TestProbe5_401RefreshRetry:
    def test_401_then_success_after_one_refresh(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "rt")
        state = {"n": 0, "refreshes": 0}

        def fake_get(url, headers, params):
            state["n"] += 1
            authz = headers.get("Authorization", "")
            if state["n"] == 1:
                # First call 401
                raise ww.reader.ReaderStopped("graph_401")
            # After refresh
            assert "good-token" in authz
            if "mailFolders" in url or "delta" in url:
                return {"value": [], "@odata.deltaLink": "https://graph/delta"}
            if url.rstrip("/").endswith("/me/chats"):
                return {"value": []}
            return {"value": []}

        def provider():
            return "stale-token"

        def refresh():
            state["refreshes"] += 1
            return "good-token"

        out = ww.reader.fetch_candidates(
            http_get=fake_get,
            token_provider=provider,
            force_refresh=refresh,
        )
        assert state["refreshes"] == 1
        assert out["ok"] is True
        assert out["graph_calls"] >= 1

    def test_401_after_refresh_stops_reader(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "rt")

        def always_401(url, headers, params):
            raise ww.reader.ReaderStopped("graph_401")

        with pytest.raises(ww.reader.ReaderStopped) as ei:
            ww.reader.fetch_candidates(
                http_get=always_401,
                token_provider=lambda: "a",
                force_refresh=lambda: "b",
            )
        assert "401" in str(ei.value).lower() or "retry" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Defect 6 — digest 8:07 CT + local-model middle (AC-P5 / AC-P7)
# ---------------------------------------------------------------------------


class TestProbe6_DigestAndLocalModel:
    def test_digest_window_is_807_ct(self, ww):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ct = ZoneInfo("America/Chicago")
        hit = datetime(2026, 10, 30, 8, 7, 10, tzinfo=ct)
        miss = datetime(2026, 10, 30, 9, 0, 0, tzinfo=ct)
        assert ww.service.is_digest_window(hit) is True
        assert ww.service.is_digest_window(miss) is False
        snap = ww.service.settings_snapshot(db_path=ww.db)
        assert snap["digest_at_ct"] == "08:07"

    def test_build_digest_counts_only_no_bodies(self, ww):
        with ww.store.open_store(ww.db) as conn:
            ww.store.upsert_flag(
                conn,
                conversation_id="d1",
                source="mail",
                tier=2,
                why="digest:fyi",
                title_local="SECRET SUBJECT",
                preview_local="SECRET BODY",
            )
        digest = ww.service.build_digest(db_path=ww.db)
        assert digest["tier_2_count"] == 1
        assert digest["message_text"] is None
        blob = str(digest)
        assert "SECRET" not in blob

    def test_local_model_middle_hook_used_for_ambiguous(self, ww):
        calls = {"n": 0}

        def local_fn(cand):
            calls["n"] += 1
            return ww.judge.Judgment(tier=2, why="local:maybe_important", rule="local_model")

        ww.judge.set_local_model_fn(local_fn)
        ww.judge.reset_local_model_budget()
        j = ww.judge.classify(
            {
                "subject": "Quarterly numbers",
                "sender": "peer@contoso.com",
                "preview": "attached spreadsheet",
                "conversation_id": "amb-1",
            }
        )
        assert calls["n"] == 1
        assert j.rule == "local_model"
        assert j.why.startswith("local:")
        assert j.dropped is False
        # Cap: five more ambiguous still respect cap
        for i in range(10):
            ww.judge.classify(
                {
                    "subject": f"Thing {i}",
                    "sender": "a@b.com",
                    "preview": "x",
                    "conversation_id": f"c{i}",
                }
            )
        assert ww.judge.local_model_calls() <= 5
