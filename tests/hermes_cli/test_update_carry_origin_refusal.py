"""A force/carry checkout must not fetch when origin is not our fork.

``hermes update`` fetches origin. On a checkout that has CARRY.md, origin
must be HudsonHawk7/hermes-agent. The official Nous origin is refused
before fetch, so an update cannot pull upstream over the fork. A checkout
with no CARRY.md is left alone, so official-repo updates still run.
"""

from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import main_install_repair
from hermes_cli import main_web_build
from hermes_cli import update_cmd

NOUS = "https://github.com/NousResearch/hermes-agent.git"
FORK = "https://github.com/HudsonHawk7/hermes-agent.git"


def _commands(store):
    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        store.append(joined)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")
        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _patch(monkeypatch, tmp_path, origin, store):
    monkeypatch.setattr(hermes_main.subprocess, "run", _commands(store))
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "CARRY.md").write_text("force/carry\n", encoding="utf-8")
    monkeypatch.setattr(hermes_main, "_resolve_update_branch", lambda args: "force/carry")
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: False)
    monkeypatch.setattr(hermes_main, "_get_origin_url", lambda *a, **k: origin)
    monkeypatch.setattr(update_cmd, "_get_origin_url", lambda *a, **k: origin)
    monkeypatch.setattr(hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(main_web_build, "_record_bytecode_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(main_install_repair, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **k: None)
    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda all_profiles=False: [])
    monkeypatch.setattr(hermes_gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(hermes_gateway, "find_profile_gateway_processes", lambda *a, **k: [])


def test_carry_checkout_refuses_nous_origin_before_fetch(monkeypatch, tmp_path, capsys):
    store = []
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch(monkeypatch, tmp_path, NOUS, store)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "origin must be HudsonHawk7/hermes-agent" in out
    assert NOUS in out
    assert not any("fetch" in cmd for cmd in store)


def test_carry_checkout_allows_fork_origin(monkeypatch, tmp_path, capsys):
    store = []
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch(monkeypatch, tmp_path, FORK, store)

    try:
        hermes_main.cmd_update(args)
    except SystemExit:
        pass

    out = capsys.readouterr().out
    assert "origin must be HudsonHawk7/hermes-agent" not in out
