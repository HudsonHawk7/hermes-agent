"""A worker cannot change the live HermesForce install. The install owner can.

RED on the base: the approval floor does not refuse a worker stop of the
desktop process, and yolo would let it through. The branch wires the floor.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tools.approval import check_all_command_guards

WORKER = "architect"
OWNER = "Architect_ClaudeCode"
STOP = "taskkill /IM Hermes.exe /F"
GATEWAY_RESTART = "hermes gateway restart"
GATEWAY_STOP = "hermes gateway stop"
REFUSAL_MARK = "file a card for Architect_ClaudeCode to land it"


def _profile(monkeypatch, name: str) -> None:
    monkeypatch.setenv("HERMES_PROFILE", name)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)


def _root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "hermes"
    (root / "hermes-agent" / "apps" / "desktop" / "release").mkdir(parents=True)
    (root / "plugins").mkdir()
    (root / "desktop-plugins").mkdir()
    return root


def _assert_refused(blocked: dict | None) -> None:
    assert blocked is not None
    assert blocked["approved"] is False
    assert "hermesforce-build-protocol" in blocked["message"]
    assert REFUSAL_MARK in blocked["message"]


def test_worker_stop_of_hermes_exe_is_refused(monkeypatch):
    _profile(monkeypatch, WORKER)
    from tools.approval import _floor_block

    blocked = _floor_block(STOP)
    _assert_refused(blocked)


def test_yolo_does_not_bypass_a_worker_stop(monkeypatch):
    _profile(monkeypatch, WORKER)
    monkeypatch.setattr("tools.approval._yolo_active", lambda: True)
    _assert_refused(check_all_command_guards(STOP, "local"))


def test_container_skip_still_refuses_a_worker_stop(monkeypatch):
    _profile(monkeypatch, WORKER)
    _assert_refused(check_all_command_guards(STOP, "docker"))


def test_install_owner_is_not_refused(monkeypatch):
    _profile(monkeypatch, OWNER)
    from tools.live_install_guard import live_install_block

    assert live_install_block(STOP, profile=OWNER, owner=OWNER) is None


def test_worker_gateway_restart_is_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    _root(tmp_path, monkeypatch)
    from tools.live_install_guard import live_install_block

    _assert_refused(live_install_block(GATEWAY_RESTART, profile=WORKER, owner=OWNER))


def test_worker_gateway_stop_is_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    _root(tmp_path, monkeypatch)
    from tools.live_install_guard import live_install_block

    _assert_refused(live_install_block(GATEWAY_STOP, profile=WORKER, owner=OWNER))


def test_worker_npm_ci_in_the_live_root_is_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    root = _root(tmp_path, monkeypatch)
    install = root / "hermes-agent"
    from tools.live_install_guard import live_install_block

    blocked = live_install_block(
        "npm ci",
        cwd=str(install),
        profile=WORKER,
        owner=OWNER,
        hermes_root=root,
    )
    _assert_refused(blocked)


def test_worker_npm_ci_in_a_worktree_is_allowed(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    root = _root(tmp_path, monkeypatch)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    from tools.live_install_guard import live_install_block

    assert live_install_block(
        "npm ci",
        cwd=str(worktree),
        profile=WORKER,
        owner=OWNER,
        hermes_root=root,
    ) is None


def test_worker_npm_prefix_pointing_at_the_live_root_is_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    root = _root(tmp_path, monkeypatch)
    install = root / "hermes-agent"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    from tools.live_install_guard import live_install_block

    blocked = live_install_block(
        "npm --prefix " + str(install) + " ci",
        cwd=str(worktree),
        profile=WORKER,
        owner=OWNER,
        hermes_root=root,
    )
    _assert_refused(blocked)


def test_worker_move_under_release_is_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    root = _root(tmp_path, monkeypatch)
    release = root / "hermes-agent" / "apps" / "desktop" / "release"
    unpacked = release / "win-unpacked"
    unpacked.mkdir()
    from tools.live_install_guard import live_install_block

    blocked = live_install_block(
        "mv " + str(unpacked) + " " + str(release / "win-unpacked.bak"),
        cwd=str(tmp_path),
        profile=WORKER,
        owner=OWNER,
        hermes_root=root,
    )
    _assert_refused(blocked)


def test_worker_write_under_plugins_is_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    root = _root(tmp_path, monkeypatch)
    target = root / "plugins" / "video-call" / "plugin.json"
    from tools.live_install_guard import write_path_block_message

    message = write_path_block_message(
        str(target), profile=WORKER, owner=OWNER, hermes_root=root,
    )
    assert message is not None
    assert "hermesforce-build-protocol" in message
    assert REFUSAL_MARK in message


def test_owner_write_under_plugins_is_not_refused(monkeypatch, tmp_path):
    _profile(monkeypatch, OWNER)
    root = _root(tmp_path, monkeypatch)
    target = root / "plugins" / "video-call" / "plugin.json"
    from tools.live_install_guard import write_path_block_message

    assert write_path_block_message(
        str(target), profile=OWNER, owner=OWNER, hermes_root=root,
    ) is None


def test_worker_read_of_the_live_tree_is_allowed(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    root = _root(tmp_path, monkeypatch)
    install = root / "hermes-agent"
    from tools.live_install_guard import live_install_block

    assert live_install_block(
        "rg live " + str(install),
        cwd=str(tmp_path),
        profile=WORKER,
        owner=OWNER,
        hermes_root=root,
    ) is None


def test_powershell_wrapper_does_not_hide_a_worker_stop(monkeypatch, tmp_path):
    _profile(monkeypatch, WORKER)
    _root(tmp_path, monkeypatch)
    from tools.live_install_guard import live_install_block

    blocked = live_install_block(
        "powershell -Command " + STOP,
        profile=WORKER,
        owner=OWNER,
    )
    _assert_refused(blocked)


def test_default_owner_is_the_install_owner_when_the_setting_is_absent():
    from tools.live_install_guard import INSTALL_OWNER_DEFAULT, install_owner_name

    assert install_owner_name({}) == INSTALL_OWNER_DEFAULT
    assert install_owner_name({"hermesforce": {"install_owner": None}}) == ""


def test_install_owner_setting_round_trips_through_ruamel(tmp_path, monkeypatch):
    """The settings snippet Architect applies must be YAML ruamel accepts."""
    from ruamel.yaml import YAML

    snippet = "hermesforce:\n  install_owner: Architect_ClaudeCode\n"
    loaded = YAML(typ="safe").load(snippet)
    assert loaded["hermesforce"]["install_owner"] == "Architect_ClaudeCode"

    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(snippet, encoding="utf-8")
    round_tripped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert round_tripped == loaded

    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.config import load_config
    from tools.live_install_guard import install_owner_name

    assert install_owner_name(load_config()) == "Architect_ClaudeCode"
