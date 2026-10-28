"""Tests: a kanban worker must never import the WORKSPACE's ``hermes_cli`` / ``pm``.

Regression coverage for the 2026-10-28 dispatcher bug (voice-hub card t_4a5ec79e died
twice in under a minute): ``_default_spawn`` launches
``-m hermes_cli.kanban_worker_client`` with ``cwd=<task workspace>``. CPython's ``-m``
prepends that cwd to ``sys.path`` *ahead of every PYTHONPATH entry*, so the
``pin_hermes_tree_on_pythonpath`` pin could not win. A task whose workspace IS a
hermes-agent checkout (every card that works in the hermes-agent worktree) therefore
booted the WORKSPACE's ``hermes_cli`` + ``pm``; ``pm.paths.repo_root()`` resolved to
that checkout, which carries no committed dependency environment, and the worker died
with ``hermes: no dependency environment is committed for this install``.

The same hole was a shadowing/security hole: any workspace holding a planted
``hermes_cli/`` ran its own code as Hermes.

The fix sets ``PYTHONSAFEPATH=1`` in the worker env (CPython 3.11+: ``-m`` no longer
prepends the cwd, and ``pm/_venv_entry`` no longer inserts ``os.getcwd()``), while the
existing PYTHONPATH pin keeps the install tree importable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Captured before any test monkeypatches the module attribute, so these tests can both
# capture the spawn argv/env and still launch real child processes.
_REAL_POPEN = subprocess.Popen


def _make_task(kb, *, assignee: str = "w"):
    return kb.Task(
        id="t_safepath",
        title="python safe path",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _capture_spawn(monkeypatch, tmp_path, workspace: Path) -> dict:
    """Run the real ``_default_spawn`` with ``Popen`` stubbed; return argv/env/cwd."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(kbd.subprocess, "Popen", fake_popen)
    kbd._default_spawn(_make_task(kb), str(workspace))
    return captured


def _run_child(argv: list[str], *, cwd: Path, env: dict) -> tuple[int, str]:
    proc = _REAL_POPEN(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out, _ = proc.communicate(timeout=180)
    return proc.returncode, out or ""


def _install_root() -> Path:
    """The tree the dispatcher pins on PYTHONPATH: the checkout it is imported from."""
    from hermes_cli import kanban_db_dispatch as kbd

    return Path(kbd.__file__).resolve().parent.parent


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def test_worker_spawn_env_sets_python_safe_path(monkeypatch, tmp_path):
    """AC1: the worker env carried into the child tells CPython not to add the cwd."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn(monkeypatch, tmp_path, workspace)

    assert captured["cwd"] == str(workspace)
    assert captured["env"].get("PYTHONSAFEPATH") == "1"
    assert captured["env"].get("HERMES_PINNED_PYTHONSAFEPATH") == "1"


def test_worker_spawn_env_keeps_the_install_tree_importable(monkeypatch, tmp_path):
    """AC1b: dropping the cwd only helps while the install tree still reaches the child."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn(monkeypatch, tmp_path, workspace)

    entries = [e for e in captured["env"].get("PYTHONPATH", "").split(os.pathsep) if e]
    assert entries, "worker PYTHONPATH lost the pin_hermes_tree_on_pythonpath entry"
    assert _same_path(Path(entries[0]), _install_root())
    assert "hermes_cli" in captured["cmd"][-1]


def test_minus_m_resolves_the_install_tree_not_the_workspace(monkeypatch, tmp_path):
    """AC2: with a package in both trees, ``-m`` runs the install tree's copy.

    The real ``hermes_cli`` cannot be used as the probe (``-m`` would boot a whole
    worker), so the fixture install tree stands in for the pinned checkout. The env
    itself is the production spawn env: PYTHONPATH keeps its production entries and the
    fixture tree is prepended, and PYTHONSAFEPATH comes from the spawn under test.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    captured = _capture_spawn(monkeypatch, tmp_path, workspace)

    install_tree = tmp_path / "install-tree"
    (install_tree / "probe_pkg").mkdir(parents=True)
    (install_tree / "probe_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (install_tree / "probe_pkg" / "__main__.py").write_text("print(__file__)\n", encoding="utf-8")
    (workspace / "probe_pkg").mkdir()
    (workspace / "probe_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "probe_pkg" / "__main__.py").write_text("print('SHADOWED')\n", encoding="utf-8")

    env = dict(captured["env"])
    env["PYTHONPATH"] = os.pathsep.join(
        [str(install_tree), *[e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e]]
    )

    code, out = _run_child([sys.executable, "-m", "probe_pkg"], cwd=workspace, env=env)

    assert code == 0, out
    resolved = Path(out.strip().splitlines()[-1])
    assert _same_path(resolved, install_tree / "probe_pkg" / "__main__.py"), out

    # Control: the same child without the flag proves the fixture really does shadow the
    # import (i.e. this test fails when the fix is reverted).
    unsafe_env = {k: v for k, v in env.items() if k != "PYTHONSAFEPATH"}
    code, out = _run_child([sys.executable, "-m", "probe_pkg"], cwd=workspace, env=unsafe_env)
    assert code == 0, out
    assert out.strip().splitlines()[-1] == "SHADOWED", out


def test_import_of_hermes_cli_comes_from_the_install_tree(monkeypatch, tmp_path):
    """AC2 on the real package: a planted workspace copy never wins."""
    workspace = tmp_path / "ws"
    (workspace / "hermes_cli").mkdir(parents=True)
    (workspace / "hermes_cli" / "__init__.py").write_text("SHADOWED = True\n", encoding="utf-8")
    captured = _capture_spawn(monkeypatch, tmp_path, workspace)
    env = dict(captured["env"])

    code, out = _run_child(
        [sys.executable, "-c", "import hermes_cli; print(hermes_cli.__file__)"],
        cwd=workspace,
        env=env,
    )

    assert code == 0, out
    assert _same_path(Path(out.strip().splitlines()[-1]), _install_root() / "hermes_cli" / "__init__.py"), out

    # Control: without the flag the workspace's planted package wins — the bug itself.
    unsafe_env = {k: v for k, v in env.items() if k != "PYTHONSAFEPATH"}
    code, out = _run_child(
        [sys.executable, "-c", "import hermes_cli; print(hermes_cli.__file__)"],
        cwd=workspace,
        env=unsafe_env,
    )
    assert code == 0, out
    assert _same_path(Path(out.strip().splitlines()[-1]), workspace / "hermes_cli" / "__init__.py"), out


def test_the_flag_stops_at_the_worker(monkeypatch, tmp_path):
    """AC3: the worker's own terminal commands keep normal CPython path behaviour.

    A worker runs the agent's `python foo.py` / `python -m pkg` inside the task
    workspace, where the script dir and the cwd belong on sys.path. The flag Hermes
    pinned must not leak into those children, only into the worker's own boot.
    """
    from tools.environments import local

    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("HERMES_PINNED_PYTHONSAFEPATH", "1")

    child_env = local.build_subprocess_env()

    assert child_env.get("PYTHONSAFEPATH") is None
    assert child_env.get("HERMES_PINNED_PYTHONSAFEPATH") is None


def test_an_operator_python_safe_path_is_not_taken_away(monkeypatch, tmp_path):
    """AC3b: only our own value is stripped — a deliberate operator value survives."""
    from tools.environments import local

    monkeypatch.setenv("PYTHONSAFEPATH", "keep-me")
    monkeypatch.delenv("HERMES_PINNED_PYTHONSAFEPATH", raising=False)

    assert local.build_subprocess_env().get("PYTHONSAFEPATH") == "keep-me"


def test_an_operator_python_safe_path_is_left_on_the_worker(monkeypatch, tmp_path):
    """AC3b on the spawn side: do not claim (and later strip) a value we did not set."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("PYTHONSAFEPATH", "1")

    captured = _capture_spawn(monkeypatch, tmp_path, workspace)

    assert captured["env"].get("PYTHONSAFEPATH") == "1"
    assert "HERMES_PINNED_PYTHONSAFEPATH" not in captured["env"]
