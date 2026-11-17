"""A desktop e2e sandbox gateway must not break away or inherit worker detach flags."""
import time

_BREAKAWAY = 0x01000000


def test_e2e_sandbox_spawn_strips_detach_flags_and_does_not_break_away(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from hermes_cli import gateway_runtime_start as start
    from hermes_cli import gateway_windows

    monkeypatch.setenv("HERMES_E2E_SANDBOX", "1")
    monkeypatch.setenv("HERMES_GATEWAY_DETACHED", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    monkeypatch.setattr(
        gateway_windows,
        "windowless_gateway_restart_spec",
        lambda command, home=None: (command, "", {
            "HERMES_GATEWAY_DETACHED": "1",
            "HERMES_SUPERVISED_CHILD": "1",
        }),
    )
    captured = {}
    child = SimpleNamespace(pid=123)

    def spawn(command, **kwargs):
        captured.update(kwargs)
        captured["command"] = command
        return child

    monkeypatch.setattr(start.subprocess, "Popen", spawn)
    assert start.spawn_unmanaged_gateway(tmp_path, deadline=time.monotonic() + 5) is child
    env = captured["env"]
    assert "HERMES_GATEWAY_DETACHED" not in env
    assert "HERMES_SUPERVISED_CHILD" not in env
    assert env.get("HERMES_E2E_SANDBOX") == "1"
    if sys.platform == "win32":
        assert captured.get("creationflags", 0) & _BREAKAWAY == 0


def test_e2e_watcher_script_compiles_and_does_not_break_away(monkeypatch):
    import sys
    from types import SimpleNamespace

    from hermes_cli import gateway as gw

    monkeypatch.setenv("HERMES_E2E_SANDBOX", "1")
    monkeypatch.setenv("HERMES_GATEWAY_DETACHED", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    captured = {}

    def spawn(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(gw.subprocess, "Popen", spawn)
    assert gw._spawn_gateway_restart_watcher(99, [sys.executable, "-m", "hermes_cli.main", "gateway", "run"])
    script = captured["argv"][2]
    compile(script, "<watcher>", "exec")
    assert "HERMES_E2E_SANDBOX" in script
    assert "windows_detach_flags_without_breakaway" in script
    if sys.platform == "win32":
        assert captured["kwargs"].get("creationflags", 0) & _BREAKAWAY == 0


def test_ordinary_spawn_still_marks_detached(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hermes_cli import gateway_runtime_start as start
    from hermes_cli import gateway_windows

    monkeypatch.delenv("HERMES_E2E_SANDBOX", raising=False)
    monkeypatch.setattr(
        gateway_windows,
        "windowless_gateway_restart_spec",
        lambda command, home=None: (command, "", {"RUNTIME_TEST_OVERLAY": "1"}),
    )
    captured = {}
    child = SimpleNamespace(pid=123)

    def spawn(command, **kwargs):
        captured.update(kwargs)
        return child

    monkeypatch.setattr(start.subprocess, "Popen", spawn)
    assert start.spawn_unmanaged_gateway(tmp_path, deadline=time.monotonic() + 5) is child
    assert captured["env"].get("HERMES_GATEWAY_DETACHED") == "1"
