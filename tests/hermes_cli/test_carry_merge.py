"""The carry-merge button must call a real command against the fork, not cwd."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.carry_merge import (
    CarryMergeError,
    FORK_OWNER,
    FORK_REF,
    command_text,
    enabled_plugin_names,
    fork_remote,
    git_argv,
    plan,
    resolve_repo,
    run_carry_merge,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _init(path: Path, branch: str) -> Path:
    path.mkdir()
    init = subprocess.run(
        ["git", "init", "-b", branch, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-b", branch],
            check=True,
            capture_output=True,
        )
    _git(path, "config", "user.email", "carry@example.com")
    _git(path, "config", "user.name", "carry")
    (path / "README").write_text("x\n", encoding="utf-8")
    assert _git(path, "add", "README").returncode == 0
    assert _git(path, "commit", "-m", "init").returncode == 0
    return path


def _remote(path: Path, name: str, url: str) -> None:
    assert _git(path, "remote", "add", name, url).returncode == 0


def test_relative_repo_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CarryMergeError, match="absolute"):
        resolve_repo("relative-checkout")


def test_wrong_branch_is_refused_without_checkout(tmp_path):
    repo = _init(tmp_path / "live", "main")
    _remote(repo, "fork", f"https://github.com/{FORK_OWNER}/hermes-agent.git")
    with pytest.raises(CarryMergeError, match="not force/carry"):
        plan(str(repo))
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"


def test_origin_is_not_the_fork_remote(tmp_path):
    repo = _init(tmp_path / "live", FORK_REF)
    _remote(repo, "origin", "https://github.com/NousResearch/hermes-agent.git")
    with pytest.raises(CarryMergeError, match="Refusing to fetch the upstream"):
        fork_remote(repo)


def test_plan_names_the_fork_and_does_not_switch(tmp_path):
    repo = _init(tmp_path / "live", FORK_REF)
    _remote(repo, "origin", "https://github.com/NousResearch/hermes-agent.git")
    _remote(repo, "fork", f"https://github.com/{FORK_OWNER}/hermes-agent.git")
    info = plan(str(repo))
    assert info["remote"] == "fork"
    assert info["checks_out_another_branch"] is False
    commands = info["commands"]
    assert isinstance(commands, list)
    joined = "\n".join(str(item) for item in commands)
    assert f"git -C {repo} fetch fork {FORK_REF}" in joined
    assert f"git -C {repo} merge --no-edit fork/{FORK_REF}" in joined
    assert "origin/main" not in joined
    assert "checkout" not in joined
    assert command_text(git_argv(repo, "fetch", "fork", FORK_REF)) == commands[0]


def test_rebuild_failure_is_not_success(tmp_path, monkeypatch, capsys):
    repo = _init(tmp_path / "live", FORK_REF)
    _remote(repo, "fork", f"https://github.com/{FORK_OWNER}/hermes-agent.git")

    def fail_rebuild(path: Path) -> None:
        raise CarryMergeError(f"FAILED: rebuild exited 2 in {path}.", 2)

    monkeypatch.setattr("hermes_cli.carry_merge._run_step", lambda *_a, **_k: None)
    monkeypatch.setattr("hermes_cli.carry_merge._rebuild", fail_rebuild)
    monkeypatch.setattr(
        "hermes_cli.carry_merge.enabled_plugin_names",
        lambda: ["scene-builder"],
    )
    code = run_carry_merge(type("Args", (), {"repo": str(repo)})())
    captured = capsys.readouterr()
    assert code == 2
    assert "FAILED: rebuild exited 2" in captured.err
    assert "plugins.enabled: scene-builder" in captured.out
    assert "Every step exited 0" not in captured.out


def test_plugins_enabled_missing_is_none_and_a_bad_shape_fails(monkeypatch):
    def empty() -> dict[str, object]:
        return {}

    def bad() -> dict[str, object]:
        return {"plugins": {"enabled": "scene-builder"}}

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", empty)
    assert enabled_plugin_names() == []
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", bad)
    with pytest.raises(CarryMergeError, match="not a list"):
        enabled_plugin_names()


def test_spawned_carry_merge_command_resolves(tmp_path):
    from hermes_cli.main import _build_cli_parser
    from hermes_cli.web_routers.ops import carry_merge_spawn_argv

    argv = carry_merge_spawn_argv(tmp_path)
    parser, _subs = _build_cli_parser()
    args = parser.parse_args(argv)
    assert args.func.__name__ == "cmd_carry_merge"
    assert args.repo == str(tmp_path)
    assert "origin/main" not in argv
    assert Path(args.repo).is_absolute()


def test_route_refuses_before_spawn(monkeypatch):
    from hermes_cli.carry_merge import CarryMergeError
    from hermes_cli.web_routers import ops

    def refuse(_explicit=None):
        raise CarryMergeError("live install is on main, not force/carry")

    spawned: list[list[str]] = []
    monkeypatch.setattr("hermes_cli.carry_merge.plan", refuse)
    monkeypatch.setattr(
        ops,
        "_spawn_action",
        lambda argv, *_a, **_k: spawned.append(list(argv)),
    )
    with pytest.raises(Exception, match="not force/carry"):
        import asyncio

        asyncio.run(ops.run_carry_merge())
    assert spawned == []
