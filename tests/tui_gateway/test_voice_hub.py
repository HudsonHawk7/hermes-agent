"""VH1 — the Voice Hub is the one owner of the microphone (design t_e850e163).

These tests drive the real gateway dispatch path and the real ``tools.wake_word`` detector. Only
the audio backend (``_import_audio``), the device-diagnostic probe and the wake engine are faked;
the fake backend records every ``InputStream`` it is asked to open, so "the microphone opened
once" is a count, not an inference.

The machine lease file is redirected to a temp dir (``HERMES_GATEWAY_LOCK_DIR``) so a test never
contends with the gateway the GM is actually using.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import types

import numpy as np
import pytest
from pydantic import ValidationError

from tools import wake_word
from tui_gateway import server, voice_hub
from tui_gateway.contracts import prompt_voice
from tui_gateway.transport import bind_transport, reset_transport

FIELDS = {"status", "owner_surface", "since", "stop_reason"}


# ── fakes: the audio backend is the only thing that isn't real ─────────────────────────────────


class _FakeEngine:
    """Stands in for the wake-word model: never fires, so nothing beyond the arm happens."""

    frame_length = 1280

    def process(self, frame) -> bool:
        return False

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeStream:
    """One PortAudio input stream. Building it IS opening the device — that is what we count."""

    def __init__(self, opens: list, **kwargs) -> None:
        opens.append(kwargs)
        self.samplerate = kwargs.get("samplerate", wake_word.SAMPLE_RATE)
        self.closed = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def abort(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def read(self, frames: int, **kwargs):
        threading.Event().wait(0.005)  # keep the reader loop cheap for the length of a test
        return np.zeros((frames, 1), dtype="int16"), False


@pytest.fixture
def mic(monkeypatch, tmp_path):
    """A real detector over a fake microphone. Yields the list of device opens (one dict each)."""
    opens: list = []
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "machine-locks"))
    monkeypatch.setattr(
        wake_word, "_import_audio",
        lambda: (types.SimpleNamespace(InputStream=lambda **kw: _FakeStream(opens, **kw)), np))
    monkeypatch.setattr(wake_word, "_describe_input_device", lambda *a, **k: {"name": "fake-mic"})
    monkeypatch.setattr(wake_word, "_build_engine", lambda cfg: _FakeEngine())
    monkeypatch.setattr(wake_word, "load_wake_word_config",
                        lambda: {"enabled": True, "capture": "local", "phrase": "hey hermes"})
    monkeypatch.setattr(wake_word, "wake_surface_enabled", lambda surface, cfg: True)
    monkeypatch.setattr(wake_word, "check_wake_word_requirements",
                        lambda cfg=None, **kw: {"available": True, "phrase": "hey hermes",
                                                "provider": "fake", "local_input_available": True,
                                                "hint": ""})
    yield opens
    voice_hub.hub().reset()
    if wake_word._current_detector() is not None:  # a real teardown, lease included
        wake_word.stop_listening(owner=wake_word._detector_owner)


def _rpc(req: dict, transport=None) -> dict:
    """One RPC to completion with the transport the gateway binds around a real request."""
    token = bind_transport(transport if transport is not None else types.SimpleNamespace(_closed=False))
    try:
        return server.handle_request(req)
    finally:
        reset_transport(token)


def _wake_start(surface: str) -> dict:
    return {"id": f"wake-{surface}", "method": "wake.start", "params": {"surface": surface}}


def _state() -> dict:
    return _rpc({"id": "vh-state", "method": "voice_hub.state", "params": {}})["result"]


def _transport() -> types.SimpleNamespace:
    return types.SimpleNamespace(_closed=False)


# ── AC4: exactly four fields, pinned by the contract ───────────────────────────────────────────


def test_voice_hub_state_is_exactly_four_fields(mic):
    payload = _state()
    assert set(payload) == FIELDS
    assert payload == {"status": "idle", "owner_surface": None, "since": payload["since"],
                       "stop_reason": None}

    # The contract is closed: a client can rely on the shape, and a fifth field fails loudly.
    prompt_voice.VoiceHubStateResult.model_validate(payload)
    with pytest.raises(ValidationError):
        prompt_voice.VoiceHubStateResult.model_validate({**payload, "listening": True})

    # The state machine is exactly the three published statuses.
    assert [s.value for s in prompt_voice.VoiceHubStatus] == ["armed", "idle", "stopped"]

    assert _rpc(_wake_start("tui"), transport=_transport())["result"]["started"] is True
    armed = _state()
    assert set(armed) == FIELDS
    assert (armed["status"], armed["owner_surface"], armed["stop_reason"]) == ("armed", "tui", None)
    assert armed["since"] >= payload["since"]

    assert _rpc({"id": "wake-stop", "method": "wake.stop", "params": {}},
                transport=types.SimpleNamespace(_closed=False))["result"]["stopped"] is False
    stopped = _state()
    assert set(stopped) == FIELDS
    assert (stopped["status"], stopped["owner_surface"], stopped["stop_reason"]) == (
        "armed", "tui", None)  # a non-owner's stop changes nothing


# ── AC2: two claimants, one owner, one device open ─────────────────────────────────────────────


def test_two_surfaces_get_one_owner_and_open_the_device_once(mic):
    tui, gui = _transport(), _transport()

    # Concurrently, as two surfaces in the one gateway really arrive.
    results: dict = {}
    barrier = threading.Barrier(2)

    def arm(surface: str, transport) -> None:
        barrier.wait(timeout=10)
        results[surface] = _rpc(_wake_start(surface), transport=transport)["result"]

    threads = [threading.Thread(target=arm, args=("tui", tui)),
               threading.Thread(target=arm, args=("gui", gui))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    started = [s for s, r in results.items() if r["started"]]
    refused = [r for r in results.values() if not r["started"]]
    assert len(started) == 1, results
    assert len(refused) == 1 and refused[0]["reason"] == "owned", results
    assert refused[0]["owner_surface"] == started[0]

    # The refusal is the detector's own typed error, not a string convention.
    winner = started[0]
    assert _state()["status"] == "armed" and _state()["owner_surface"] == winner
    with pytest.raises(wake_word.WakeWordInUse):
        voice_hub.hub().claim("gui" if winner == "tui" else "tui", _transport())

    # …and the input device was opened exactly once for the pair.
    assert len(mic) == 1
    assert wake_word.owns_listener(voice_hub.hub().snapshot()[0]) is True


# ── AC1 + AC3: a config value never arms anything, and idle never opens the mic ────────────────


def test_idle_hub_opens_nothing_for_ten_minutes_with_wake_word_enabled(mic):
    """``wake_word.enabled: true`` + ten idle minutes = 0 capture sessions, so 0 transcriptions."""
    now = [1000.0]
    hub = voice_hub.VoiceHub(clock=lambda: now[0])
    for _ in range(600):
        now[0] += 1.0
        assert hub.state()["status"] == "idle"
    assert mic == []                       # zero capture sessions
    assert wake_word._current_detector() is None
    assert _state()["status"] == "idle"    # the gateway's own Hub, same answer


def test_arming_is_not_restored_from_config_after_a_restart(mic):
    assert _rpc(_wake_start("tui"), transport=_transport())["result"]["started"] is True
    assert _state()["status"] == "armed"
    assert wake_word.load_wake_word_config()["enabled"] is True

    # A restart: the process forgets every claim, and the detector dies with the old process.
    wake_word.stop_listening(owner=voice_hub.hub().snapshot()[0])
    voice_hub.hub().reset()

    fresh = _state()
    assert fresh["status"] == "idle"
    assert fresh["owner_surface"] is None
    assert wake_word.load_wake_word_config()["enabled"] is True  # config still says on…
    assert wake_word._current_detector() is None                 # …and nothing is listening
    assert len(mic) == 1                                         # no device re-opened by itself


# ── AC5: the lease is an OS-level named lock, so another HERMES_HOME is still refused ──────────


def test_mic_lease_path_does_not_follow_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_GATEWAY_LOCK_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-a"))
    first = wake_word._lock_path()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home-b"))
    assert wake_word._lock_path() == first


def test_second_gateway_on_a_scratch_hermes_home_is_refused_with_one_device_open(mic, tmp_path):
    assert _rpc(_wake_start("tui"), transport=_transport())["result"]["started"] is True
    assert len(mic) == 1

    scratch_home = tmp_path / "scratch-home"
    scratch_home.mkdir()
    (scratch_home / "config.yaml").write_text("wake_word:\n  enabled: true\n", encoding="utf-8")
    child = textwrap.dedent(
        """
        import json, types
        import numpy as np
        import tools.wake_word as ww

        opens = []

        class _Engine:
            frame_length = 1280
            def process(self, frame): return False
            def reset(self): pass
            def close(self): pass

        class _Stream:
            samplerate = 16000
            def __init__(self, **kw): opens.append(kw)
            def start(self): pass
            def stop(self): pass
            def abort(self): pass
            def close(self): pass
            def read(self, frames, **kw): return np.zeros((frames, 1), dtype="int16"), False

        ww._import_audio = lambda: (types.SimpleNamespace(InputStream=_Stream), np)
        ww._describe_input_device = lambda *a, **k: {"name": "fake-mic"}
        ww._build_engine = lambda cfg: _Engine()
        ww.load_wake_word_config = lambda: {"enabled": True, "capture": "local"}
        ww.wake_surface_enabled = lambda surface, cfg: True
        ww.check_wake_word_requirements = lambda cfg=None, **kw: {
            "available": True, "phrase": "hey hermes", "provider": "fake",
            "local_input_available": True, "hint": ""}

        from tui_gateway import server
        from tui_gateway.transport import bind_transport, reset_transport
        token = bind_transport(types.SimpleNamespace(_closed=False))
        try:
            reply = server.handle_request({"id": "1", "method": "wake.start",
                                          "params": {"surface": "tui"}})
        finally:
            reset_transport(token)
        print("RESULT " + json.dumps(reply["result"], sort_keys=True))
        print("OPENS %d" % len(opens))
        """
    )
    env = {**os.environ, "HERMES_HOME": str(scratch_home)}
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run([sys.executable, "-c", child], cwd=repo_root, env=env,
                          capture_output=True, text=True, timeout=300, check=False)

    # The gateway owns stdout for JSON-RPC framing, so a child process's prints land on stderr.
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-4000:]
    result = json.loads(output.split("RESULT ", 1)[1].splitlines()[0])
    assert result["started"] is False, output
    assert result["reason"] == "owned", output          # the detector's typed WakeWordInUse
    assert "OPENS 0" in output, output                  # refused before the device was touched
    assert len(mic) == 1                                # the first owner still holds the one open
