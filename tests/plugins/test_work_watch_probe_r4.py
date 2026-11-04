"""Probe round-4 contract defect for work_watch Path A (AC-N11).

Round-3 still called the local-model client when base_url was empty/missing,
so subject and preview left the local guard. Cloud URL already refused.
Empty/missing base must refuse with model_unavailable and zero client calls.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

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


def _fake_aux_module(client, model="local-test"):
    mod = types.ModuleType("agent.auxiliary_client")

    def get_text_auxiliary_client(task="work_watch_judge"):
        return client, model

    mod.get_text_auxiliary_client = get_text_auxiliary_client
    return mod


class TestR4_EmptyBaseRefuses:
    """AC-N11: empty/missing base_url must not call the client with subject/preview."""

    def test_empty_base_url_does_not_call_client(self, ww):
        calls = {"n": 0}
        prompts: list[str] = []

        class _Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                content = ""
                try:
                    content = kwargs["messages"][0]["content"]
                except Exception:
                    content = str(kwargs)
                prompts.append(content)
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(content="TIER 2 WHY should_not_run")
                        )
                    ]
                )

        client = types.SimpleNamespace(
            base_url="",
            chat=types.SimpleNamespace(completions=_Completions()),
        )
        aux = _fake_aux_module(client)
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.auxiliary_client"] = aux

        ww.judge.install_production_local_model()
        j = ww.judge.classify(
            {
                "subject": "SECRET_SUBJECT_SHOULD_NOT_LEAVE",
                "sender": "peer@contoso.com",
                "preview": "SECRET_PREVIEW_SHOULD_NOT_LEAVE",
                "conversation_id": "ac-n11-empty-base",
            }
        )

        assert calls["n"] == 0, (
            f"empty base_url still called the model {calls['n']} time(s); "
            f"subject/preview must not leave the guard (prompts={prompts!r})"
        )
        assert j.why == "model_unavailable"
        assert j.rule == "local_model_unavailable"
        assert j.dropped is False
        assert j.tier == 2
        assert "SECRET_SUBJECT" not in "".join(prompts)
        assert "SECRET_PREVIEW" not in "".join(prompts)

    def test_whitespace_base_url_does_not_call_client(self, ww):
        calls = {"n": 0}

        class _Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(content="TIER 2 WHY no")
                        )
                    ]
                )

        client = types.SimpleNamespace(
            base_url="   ",
            chat=types.SimpleNamespace(completions=_Completions()),
        )
        aux = _fake_aux_module(client)
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.auxiliary_client"] = aux

        ww.judge.install_production_local_model()
        j = ww.judge.classify(
            {
                "subject": "Ambiguous thread",
                "sender": "peer@contoso.com",
                "preview": "when free",
                "conversation_id": "ac-n11-ws-base",
            }
        )
        assert calls["n"] == 0
        assert j.why == "model_unavailable"
        assert j.dropped is False

    def test_missing_base_attr_does_not_call_client(self, ww):
        """Client with no base_url attribute must refuse, not fail-open."""
        calls = {"n": 0}

        class _Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                return MagicMock()

        # No base_url attribute at all
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))
        aux = _fake_aux_module(client)
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.auxiliary_client"] = aux

        ww.judge.install_production_local_model()
        j = ww.judge.classify(
            {
                "subject": "No base attr mail",
                "sender": "peer@contoso.com",
                "preview": "body",
                "conversation_id": "ac-n11-no-attr",
            }
        )
        assert calls["n"] == 0
        assert j.why == "model_unavailable"

    def test_cloud_base_still_refused(self, ww):
        calls = {"n": 0}

        class _Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                return MagicMock()

        client = types.SimpleNamespace(
            base_url="https://api.openai.com/v1",
            chat=types.SimpleNamespace(completions=_Completions()),
        )
        aux = _fake_aux_module(client)
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.auxiliary_client"] = aux

        ww.judge.install_production_local_model()
        j = ww.judge.classify(
            {
                "subject": "Cloud refuse check",
                "sender": "peer@contoso.com",
                "preview": "body",
                "conversation_id": "ac-n11-cloud",
            }
        )
        assert calls["n"] == 0
        assert j.why == "model_unavailable"

    def test_local_base_still_calls(self, ww):
        """Regression: a clearly-local base must still be allowed to call once."""
        calls = {"n": 0}

        class _Completions:
            def create(self, **kwargs):
                calls["n"] += 1
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(content="TIER 2 WHY local_ok")
                        )
                    ]
                )

        client = types.SimpleNamespace(
            base_url="http://127.0.0.1:11434/v1",
            chat=types.SimpleNamespace(completions=_Completions()),
        )
        aux = _fake_aux_module(client)
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.auxiliary_client"] = aux

        ww.judge.install_production_local_model()
        j = ww.judge.classify(
            {
                "subject": "Local ok mail",
                "sender": "peer@contoso.com",
                "preview": "body",
                "conversation_id": "ac-n11-local-ok",
            }
        )
        assert calls["n"] == 1
        assert j.why == "local:local_ok"
        assert j.tier == 2
        assert j.dropped is False
