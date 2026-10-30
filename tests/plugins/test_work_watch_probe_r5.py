"""Probe round-5 contract defect for work_watch Path A (AC-N11).

Round-4 still treated a hosted URL as local when the base string merely
contained a hint word (local, ollama, 11434, localhost, 127.0.0.1), so
subject and preview left the guard. Parse the host; allow only real
loopback (localhost, 127.0.0.1, ::1). Hosted hosts must refuse with
model_unavailable and zero client calls.
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


def _tracking_client(base_url: str):
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
        base_url=base_url,
        chat=types.SimpleNamespace(completions=_Completions()),
    )
    return client, calls, prompts


SECRET_SUBJECT = "SECRET_SUBJECT_SHOULD_NOT_LEAVE"
SECRET_PREVIEW = "SECRET_PREVIEW_SHOULD_NOT_LEAVE"


def _classify_with(ww, base_url: str):
    client, calls, prompts = _tracking_client(base_url)
    aux = _fake_aux_module(client)
    sys.modules["agent"] = types.ModuleType("agent")
    sys.modules["agent.auxiliary_client"] = aux
    ww.judge.install_production_local_model()
    j = ww.judge.classify(
        {
            "subject": SECRET_SUBJECT,
            "sender": "peer@contoso.com",
            "preview": SECRET_PREVIEW,
            "conversation_id": f"ac-n11-host-{abs(hash(base_url)) % 10_000_000}",
        }
    )
    return j, calls, prompts


class TestR5_HostParseOnlyLoopback:
    """AC-N11: only real loopback hostnames may call the client."""

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.mylocal.ai/v1",  # hostname contains 'local'
            "https://localhost.evil.example/v1",  # prefix, not exact host
            "https://not-a-local-host.example.com/v1",  # substring 'local'
            "https://proxy.example.com/ollama/v1",  # path contains 'ollama'
            "https://evil.example:11434/v1",  # port 11434 is not a host check
            "http://127.0.0.1.evil.com/v1",  # dotted prefix, not 127.0.0.1
            "http://127.0.0.1:11434@evil.com/v1",  # userinfo trick; host is evil.com
        ],
    )
    def test_hosted_hint_urls_do_not_call_client(self, ww, base_url):
        j, calls, prompts = _classify_with(ww, base_url)
        assert calls["n"] == 0, (
            f"hosted base {base_url!r} still called the model {calls['n']} time(s); "
            f"subject/preview must not leave the guard (prompts={prompts!r})"
        )
        assert j.why == "model_unavailable"
        assert j.rule == "local_model_unavailable"
        assert j.dropped is False
        assert j.tier == 2
        joined = "".join(prompts)
        assert SECRET_SUBJECT not in joined
        assert SECRET_PREVIEW not in joined

    def test_api_mylocal_ai_explicit(self, ww):
        """Named Probe case: https://api.mylocal.ai/v1 must refuse."""
        j, calls, prompts = _classify_with(ww, "https://api.mylocal.ai/v1")
        assert calls["n"] == 0
        assert j.why == "model_unavailable"
        assert SECRET_SUBJECT not in "".join(prompts)

    def test_evil_port_11434_explicit(self, ww):
        """Named Probe case: https://evil.example:11434/v1 must refuse."""
        j, calls, prompts = _classify_with(ww, "https://evil.example:11434/v1")
        assert calls["n"] == 0
        assert j.why == "model_unavailable"
        assert SECRET_SUBJECT not in "".join(prompts)

    def test_userinfo_loopback_trick_explicit(self, ww):
        """Named Probe case: http://127.0.0.1:11434@evil.com/v1 must refuse."""
        j, calls, prompts = _classify_with(ww, "http://127.0.0.1:11434@evil.com/v1")
        assert calls["n"] == 0
        assert j.why == "model_unavailable"
        assert SECRET_SUBJECT not in "".join(prompts)

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://127.0.0.1:11434/v1",
            "http://localhost:11434/v1",
            "http://[::1]:11434/v1",
        ],
    )
    def test_real_loopback_still_calls(self, ww, base_url):
        """Regression: exact loopback hosts still get one local call."""
        client, calls, _prompts = _tracking_client(base_url)

        class _Ok:
            def create(self, **kwargs):
                calls["n"] += 1
                return types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(content="TIER 2 WHY local_ok")
                        )
                    ]
                )

        client.chat = types.SimpleNamespace(completions=_Ok())
        aux = _fake_aux_module(client)
        sys.modules["agent"] = types.ModuleType("agent")
        sys.modules["agent.auxiliary_client"] = aux
        ww.judge.install_production_local_model()
        j = ww.judge.classify(
            {
                "subject": "Local ok mail",
                "sender": "peer@contoso.com",
                "preview": "body",
                "conversation_id": f"ac-n11-loop-{abs(hash(base_url)) % 10_000_000}",
            }
        )
        assert calls["n"] == 1, f"loopback {base_url!r} should call once, got {calls['n']}"
        assert j.why == "local:local_ok"
        assert j.tier == 2
        assert j.dropped is False

    def test_empty_base_still_refused(self, ww):
        """r4 regression: empty base still refuses."""
        j, calls, _prompts = _classify_with(ww, "")
        assert calls["n"] == 0
        assert j.why == "model_unavailable"
