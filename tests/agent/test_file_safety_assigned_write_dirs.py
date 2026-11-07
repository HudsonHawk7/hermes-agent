"""Hermes must never point a worker at a folder and then refuse the write.

The kanban dispatcher hands every worker a task workspace (``HERMES_KANBAN_WORKSPACE``)
and a run temp folder under ``kanban.worker_temp_root`` (``HERMES_WORKER_TEMP_ROOT``).
With an operator ``HERMES_WRITE_SAFE_ROOT`` set, both must stay writable, while every
other path outside the operator's list is still refused.
"""

import os
from unittest.mock import patch

from agent.file_safety import get_safe_write_roots, get_write_denied_error, is_write_denied


def _env(safe_root, workspace=None, worker_temp=None):
    env = {"HERMES_WRITE_SAFE_ROOT": str(safe_root)}
    if workspace is not None:
        env["HERMES_KANBAN_WORKSPACE"] = str(workspace)
    if worker_temp is not None:
        env["HERMES_WORKER_TEMP_ROOT"] = str(worker_temp)
    return env


def _clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("HERMES_WRITE_SAFE_ROOT", "HERMES_KANBAN_WORKSPACE", "HERMES_WORKER_TEMP_ROOT")}


def test_task_workspace_is_writable_under_an_operator_safe_root(tmp_path):
    safe, workspace = tmp_path / "agent-home", tmp_path / "kanban" / "workspaces" / "t_abc"
    safe.mkdir()
    workspace.mkdir(parents=True)
    with patch.dict(os.environ, {**_clean_env(), **_env(safe, workspace=workspace)}, clear=True):
        assert not is_write_denied(str(workspace / "MANIFEST.md"))
        assert get_write_denied_error(str(workspace / "pr-body.md")) is None


def test_worker_temp_folder_is_writable_under_an_operator_safe_root(tmp_path):
    safe, run_tmp = tmp_path / "agent-home", tmp_path / "_tmp" / "probe" / "t_abc_7"
    safe.mkdir()
    run_tmp.mkdir(parents=True)
    with patch.dict(os.environ, {**_clean_env(), **_env(safe, worker_temp=run_tmp)}, clear=True):
        assert not is_write_denied(str(run_tmp / "crop_shots.txt"))


def test_paths_outside_every_root_are_still_refused(tmp_path):
    safe, workspace, run_tmp = tmp_path / "agent-home", tmp_path / "ws" / "t_abc", tmp_path / "_tmp" / "t_abc_7"
    for d in (safe, workspace, run_tmp):
        d.mkdir(parents=True)
    outside = tmp_path / "someone-elses-repo" / "src.py"
    sibling = tmp_path / "ws" / "t_other" / "steal.md"
    with patch.dict(os.environ, {**_clean_env(), **_env(safe, workspace, run_tmp)}, clear=True):
        assert is_write_denied(str(outside))
        assert is_write_denied(str(sibling)), "another card's workspace must stay refused"
        assert "outside HERMES_WRITE_SAFE_ROOT" in get_write_denied_error(str(outside))


def test_assigned_dirs_do_not_create_a_restriction_when_safe_root_is_unset(tmp_path):
    workspace = tmp_path / "ws" / "t_abc"
    workspace.mkdir(parents=True)
    env = {**_clean_env(), "HERMES_KANBAN_WORKSPACE": str(workspace)}
    with patch.dict(os.environ, env, clear=True):
        assert get_safe_write_roots() == set()
        assert not is_write_denied(str(tmp_path / "anywhere.txt"))


def test_relative_or_empty_assigned_values_are_ignored(tmp_path):
    safe = tmp_path / "agent-home"
    safe.mkdir()
    env = {**_clean_env(), "HERMES_WRITE_SAFE_ROOT": str(safe),
           "HERMES_KANBAN_WORKSPACE": "relative/ws", "HERMES_WORKER_TEMP_ROOT": "  "}
    with patch.dict(os.environ, env, clear=True):
        assert get_safe_write_roots() == {os.path.realpath(str(safe))}
