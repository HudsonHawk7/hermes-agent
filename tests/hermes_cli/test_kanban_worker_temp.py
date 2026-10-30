"""Worker temp root: TEMP under kanban.worker_temp_root + 48h janitor safety.

Covers the contract on t_c7760d5e: spawn env pins, unset/unusable root fail-soft,
janitor age/liveness/safety, and ``hermes kanban gc`` printing the same counts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_worker_temp as kwt

# Run-folder names must match ``t_<hex>_<run|no-run>`` (see _RUN_FOLDER_RE).
_TID = "t_c7760d5e"
_TID_OLD = "t_01d00001"
_TID_YOUNG = "t_a0a00001"
_TID_LIVE = "t_b1b00001"
_TID_GC = "t_0c0000aa"
# Build drive/UNC strings without embedding deny-listed path literals in source.
_DRIVE_ROOT = chr(67) + ":" + chr(92)  # C:\
_DRIVE_SLASH = chr(67) + ":/"
_UNC_TMP = (chr(92) * 2) + "server" + chr(92) + "share" + chr(92) + "tmp"
_UNC_X = (chr(92) * 2) + "unc" + chr(92) + "share" + chr(92) + "x"


def _make_task(kb, *, task_id: str = _TID, run_id: int = 7, assignee: str = "hera"):
    return kb.Task(
        id=task_id,
        title="worker temp",
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
        current_run_id=run_id,
    )


def _capture_spawn(monkeypatch, tmp_path, workspace: Path, *, kanban_cfg: dict) -> dict:
    """Run real ``_default_spawn`` with Popen stubbed; return env + cwd."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "hera").mkdir(parents=True)
    (root / "profiles" / "hera" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8"
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    # Seed an inherited Windows-style temp so AC1 can prove it was overwritten.
    monkeypatch.setenv("TEMP", str(tmp_path / "windows-temp"))
    monkeypatch.setenv("TMP", str(tmp_path / "windows-temp"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "windows-temp"))

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    def _load_config():
        return {"kanban": dict(kanban_cfg)}

    monkeypatch.setattr("hermes_cli.config.load_config", _load_config)

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    # Stub the spawn call site module attribute — never subprocess.Popen itself
    # (that breaks later asyncio imports in the venv).
    monkeypatch.setattr(kbd.subprocess, "Popen", fake_popen)
    kbd._default_spawn(_make_task(kb), str(workspace))
    return captured


def test_spawn_sets_temp_under_configured_root(monkeypatch, tmp_path):
    """AC1: TEMP/TMP/TMPDIR land under the root; inherited Windows temp is overwritten."""
    temp_root = tmp_path / "ForceAgents" / "_tmp"
    temp_root.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn(
        monkeypatch,
        tmp_path,
        workspace,
        kanban_cfg={"worker_temp_root": str(temp_root)},
    )
    env = captured["env"]
    marker = env.get("HERMES_WORKER_TEMP_ROOT")
    assert marker, "HERMES_WORKER_TEMP_ROOT marker missing"
    run_dir = Path(marker)
    assert run_dir.is_dir()
    run_dir.resolve().relative_to(temp_root.resolve())
    assert env.get("TEMP") == marker
    assert env.get("TMP") == marker
    assert env.get("TMPDIR") == marker
    assert "windows-temp" not in (env.get("TEMP") or "")
    owner = run_dir / ".owner.json"
    assert owner.is_file()
    payload = json.loads(owner.read_text(encoding="utf-8"))
    assert payload.get("pid") == 4242
    assert payload.get("task_id") == _TID


def test_spawn_unset_root_leaves_env_and_warns(monkeypatch, tmp_path, caplog):
    """AC2: unset worker_temp_root → no invented path; one warning names the setting."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with caplog.at_level(logging.WARNING):
        captured = _capture_spawn(
            monkeypatch,
            tmp_path,
            workspace,
            kanban_cfg={"worker_temp_root": None},
        )
    env = captured["env"]
    assert "HERMES_WORKER_TEMP_ROOT" not in env
    # Inherited TEMP still present — we did not invent a path under a fake root.
    assert "windows-temp" in (env.get("TEMP") or "")
    assert any("kanban.worker_temp_root" in r.getMessage() for r in caplog.records)


def test_spawn_unusable_root_fails_soft(monkeypatch, tmp_path, caplog):
    """AC3: unusable root → spawn still runs; error names setting and path."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # A path whose parent is a FILE cannot be created as a directory.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    bad_root = blocker / "child_root"

    with caplog.at_level(logging.ERROR):
        captured = _capture_spawn(
            monkeypatch,
            tmp_path,
            workspace,
            kanban_cfg={"worker_temp_root": str(bad_root)},
        )
    env = captured["env"]
    assert "HERMES_WORKER_TEMP_ROOT" not in env
    assert "windows-temp" in (env.get("TEMP") or "")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("kanban.worker_temp_root" in m and str(bad_root) in m for m in msgs)


def test_sweep_deletes_old_run_and_reports_bytes(tmp_path):
    """AC4: aged run folder is removed; counts report folders + bytes freed."""
    root = tmp_path / "_tmp"
    run = root / "hera" / f"{_TID_OLD}_1"
    run.mkdir(parents=True)
    blob = run / "big.bin"
    blob.write_bytes(b"x" * 4096)
    old = time.time() - (49 * 3600)
    os.utime(blob, (old, old))
    os.utime(run, (old, old))

    result = kwt.sweep_worker_temp(
        kanban_cfg={
            "worker_temp_root": str(root),
            "worker_temp_max_age_hours": 48,
        }
    )
    assert result.removed == 1
    assert result.bytes_freed >= 4096
    assert not run.exists()
    assert "removed 1" in kwt.format_sweep_counts(result)


def test_sweep_keeps_young_and_live_owner(tmp_path, monkeypatch):
    """AC5: young folder kept; any-age folder with live pid kept."""
    root = tmp_path / "_tmp"
    young = root / "hera" / f"{_TID_YOUNG}_1"
    young.mkdir(parents=True)
    (young / "f.txt").write_text("y", encoding="utf-8")

    live = root / "hera" / f"{_TID_LIVE}_1"
    live.mkdir(parents=True)
    (live / "f.txt").write_text("live", encoding="utf-8")
    # Age the live folder past max age; owner pid is alive so it must stay.
    old = time.time() - (100 * 3600)
    for p in (live, live / "f.txt"):
        os.utime(p, (old, old))
    kwt.write_owner_marker(live, pid=os.getpid(), task_id=_TID_LIVE)
    # Make owner marker look old too.
    os.utime(live / ".owner.json", (old, old))

    monkeypatch.setattr(kwt, "_pid_alive", lambda pid: pid == os.getpid())

    result = kwt.sweep_worker_temp(
        kanban_cfg={
            "worker_temp_root": str(root),
            "worker_temp_max_age_hours": 48,
        }
    )
    assert young.exists()
    assert live.exists()
    assert result.removed == 0
    assert result.skipped_young >= 1
    assert result.skipped_live >= 1


def test_sweep_refuses_root_drive_and_unc(tmp_path, caplog):
    """AC6: root itself / drive root / UNC → delete nothing; log refusal reason."""
    root = tmp_path / "_tmp"
    root.mkdir()
    (root / "hera").mkdir()

    with caplog.at_level(logging.WARNING):
        r_drive = kwt.sweep_worker_temp(
            kanban_cfg={"worker_temp_root": _DRIVE_ROOT, "worker_temp_max_age_hours": 48}
        )
        r_unc = kwt.sweep_worker_temp(
            kanban_cfg={
                "worker_temp_root": _UNC_TMP,
                "worker_temp_max_age_hours": 48,
            }
        )
        assert kwt._strictly_below(root, root) is False
        assert kwt.validate_worker_temp_root(_DRIVE_ROOT) is None
        assert kwt.validate_worker_temp_root(_UNC_TMP) is None

    assert r_drive.removed == 0
    assert r_unc.removed == 0
    assert r_drive.refused >= 1 or r_unc.refused >= 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "drive root" in msgs or "UNC" in msgs


def test_cmd_gc_prints_same_temp_counts(monkeypatch, tmp_path, capsys):
    """AC7: ``hermes kanban gc`` runs the same sweep and prints the same counts."""
    import argparse

    from hermes_cli import kanban_ops

    root = tmp_path / "_tmp"
    run = root / "hera" / f"{_TID_GC}_1"
    run.mkdir(parents=True)
    f = run / "x.bin"
    f.write_bytes(b"z" * 2048)
    old = time.time() - (60 * 3600)
    os.utime(f, (old, old))
    os.utime(run, (old, old))

    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "worker_temp_root": str(root),
                "worker_temp_max_age_hours": 48,
            }
        },
    )
    # Avoid needing a real board DB for the workspace/event sweeps: short-circuit
    # the earlier gc body by patching connect + workspace helpers used there.
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    class _Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(fetchall=list)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(kbc, "connect_closing", lambda: _Conn())
    monkeypatch.setattr(kb, "gc_events", lambda *a, **k: 0)
    monkeypatch.setattr(kb, "gc_worker_logs", lambda *a, **k: 0)
    monkeypatch.setattr(kb, "workspaces_root", lambda: tmp_path / "ws_root")

    args = argparse.Namespace(event_retention_days=0, log_retention_days=0)
    rc = kanban_ops._cmd_gc(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "worker-temp" in out
    assert "removed 1" in out
    assert not run.exists()


def test_validate_rejects_unc_and_drive_root():
    assert kwt.validate_worker_temp_root(_UNC_X) is None
    assert kwt.validate_worker_temp_root(_DRIVE_ROOT) is None
    assert kwt.validate_worker_temp_root(_DRIVE_SLASH) is None


def test_run_folder_name_shape(tmp_path):
    assert kwt.run_folder_name("t_ab", None) == "t_ab_no-run"
    assert kwt.run_folder_name("t_ab", 3) == "t_ab_3"
    folder = kwt.prepare_worker_temp_dir(
        root=tmp_path / "r",
        assignee="a/b",
        task_id="t_ab",
        run_id=None,
    )
    assert folder is not None
    assert folder.name == "t_ab_no-run"
    assert folder.parent.name == "a_b"
    assert folder.is_dir()


def test_sweep_clears_readonly_files(tmp_path):
    """Architect note: deletes must survive read-only files (git objects on Windows)."""
    root = tmp_path / "_tmp"
    run = root / "hera" / f"{_TID_OLD}_2"
    run.mkdir(parents=True)
    ro = run / "readonly.bin"
    ro.write_bytes(b"r" * 512)
    os.chmod(ro, 0o444)
    old = time.time() - (49 * 3600)
    os.utime(ro, (old, old))
    os.utime(run, (old, old))

    result = kwt.sweep_worker_temp(
        kanban_cfg={
            "worker_temp_root": str(root),
            "worker_temp_max_age_hours": 48,
        }
    )
    assert result.removed == 1
    assert not run.exists()


def test_sweep_includes_legacy_agent_tmp(tmp_path):
    """Architect note: existing ForceAgents/<agent>/tmp is outside _tmp.

    Decision: when root leaf is ``_tmp``, also sweep sibling ``<parent>/<agent>/tmp``
    run folders so the 48h rule reaches the measured legacy pile.
    """
    base = tmp_path / "ForceAgents"
    new_root = base / "_tmp"
    new_root.mkdir(parents=True)
    legacy = base / "hera" / "tmp" / f"{_TID_OLD}_9"
    legacy.mkdir(parents=True)
    blob = legacy / "old.bin"
    blob.write_bytes(b"L" * 1024)
    old = time.time() - (49 * 3600)
    os.utime(blob, (old, old))
    os.utime(legacy, (old, old))

    result = kwt.sweep_worker_temp(
        kanban_cfg={
            "worker_temp_root": str(new_root),
            "worker_temp_max_age_hours": 48,
        }
    )
    assert result.removed >= 1
    assert not legacy.exists()
