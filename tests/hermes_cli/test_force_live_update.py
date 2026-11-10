"""Fakes for every force-live-update gate, refusal, and rollback branch."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hermes_cli import force_live_update as flu
from hermes_cli.subcommands.force_live_update import build_force_live_update_parser

NOW = datetime(2026, 11, 9, 21, 0, 0)
TARGET = "abc123def4567890abc123def4567890abc123de"
PREV = "1111111111111111111111111111111111111111"
QUIET_LINE = "2026-11-09 20:50:00,000 INFO agent.turn_context: Turn ended: session=s model=m api_calls=1 response_model=m"


class FakeIO:
    def __init__(self):
        self.cards = []
        self.girls = {
            "solara": flu.GirlState("solara", True, "quiet 600s"),
            "aura": flu.GirlState("aura", True, "quiet 600s"),
        }
        self.head_sha = PREV
        self.ff = flu.StepResult(True, "fast-forwarded", sha=TARGET)
        self.pack = flu.StepResult(True, "packed")
        self.stamp = TARGET
        self.restart = flu.StepResult(True, "gateway restart launched")
        self.health = flu.StepResult(True, "gateway health 200 pid 2")
        self.launch = flu.StepResult(True, "launched desktop")
        self.boot = flu.StepResult(True, "boot proof ok")
        self.boot_after = flu.StepResult(True, "boot proof ok")
        self.meter = flu.StepResult(True, "MyMeter proof ok")
        self.calls = []
        self.reset_sha = None
        self.receipts = []
        self._rolled = False

    def now(self):
        return NOW

    def running_cards(self):
        self.calls.append("running_cards")
        return list(self.cards)

    def girl_states(self, now):
        self.calls.append("girl_states")
        return [self.girls["solara"], self.girls["aura"]]

    def head(self):
        self.calls.append("head")
        return self.head_sha

    def snapshot_release_prev(self):
        self.calls.append("snapshot_release_prev")
        return "release-prev"

    def snapshot_configs(self, stamp):
        self.calls.append("snapshot_configs")
        return stamp

    def ff_only(self, sha):
        self.calls.append("ff_only")
        return self.ff

    def pack_desktop(self, sha):
        self.calls.append("pack_desktop")
        return self.pack

    def stamp_commit(self):
        self.calls.append("stamp_commit")
        return self.stamp

    def swap_release(self):
        self.calls.append("swap_release")

    def restore_release_prev(self):
        self.calls.append("restore_release_prev")

    def git_reset_keep(self, sha):
        self.calls.append("git_reset_keep")
        self.reset_sha = sha

    def gateway_pid(self):
        self.calls.append("gateway_pid")
        return 1

    def restart_gateway(self):
        self.calls.append("restart_gateway")
        return self.restart

    def wait_gateway(self, old_pid):
        self.calls.append("wait_gateway")
        return self.health

    def launch_desktop(self):
        self.calls.append("launch_desktop")
        return NOW, self.launch

    def wait_boot(self, launch):
        self.calls.append("wait_boot")
        if self._rolled:
            return self.boot_after
        return self.boot

    def mymeter(self):
        self.calls.append("mymeter")
        return self.meter

    def write_receipt(self, stamp, summary, receipt):
        self.calls.append("write_receipt")
        self.receipts.append((summary, receipt))
        return f"live-update-{stamp}.log"

    def fail_and_rollback(self):
        self._rolled = False

    def mark_rolled(self):
        self._rolled = True


def _run(io, desktop=True):
    # wait_boot after rollback should see the after-state. Flip the flag
    # when rollback starts by wrapping restore.
    original = io.restore_release_prev

    def _restore():
        io._rolled = True
        return original()

    io.restore_release_prev = _restore
    return flu.run_protocol(io, target_sha=TARGET, desktop=desktop)


def test_gate_refuses_running_card_and_does_not_ff():
    io = FakeIO()
    io.cards = [flu.RunningCard("default", "t_abc", "still working")]
    outcome = _run(io)
    assert outcome.exit_code == 2
    assert "t_abc" in outcome.failed_line
    assert "still working" in outcome.failed_line
    assert "ff_only" not in io.calls
    assert "restart_gateway" not in io.calls
    assert outcome.rolled_back is False


def test_gate_refuses_when_girl_log_is_not_quiet():
    io = FakeIO()
    io.girls["aura"] = flu.GirlState("aura", False, "last line is not Turn ended: still going")
    outcome = _run(io)
    assert outcome.exit_code == 2
    assert "aura:" in outcome.failed_line
    assert "ff_only" not in io.calls


def test_gate_passes_when_quiet_and_writes_ok_receipt():
    io = FakeIO()
    outcome = _run(io, desktop=False)
    assert outcome.exit_code == 0
    assert outcome.failed_line is None
    assert "live-update ok" in outcome.summary
    assert outcome.rolled_back is False
    assert "git_reset_keep" not in io.calls
    assert "pack_desktop" not in io.calls
    assert io.receipts[-1][1]["ok"] is True


def test_refuses_when_not_a_fast_forward():
    io = FakeIO()
    io.ff = flu.StepResult(False, "refused: not a fast-forward (divergent)")
    outcome = _run(io)
    assert outcome.exit_code == 3
    assert "not a fast-forward" in outcome.failed_line
    assert "git_reset_keep" not in io.calls
    assert "swap_release" not in io.calls


def test_refuses_when_sha_is_not_on_branch():
    io = FakeIO()
    io.ff = flu.StepResult(False, f"refused: {TARGET} is not on fork/force/carry")
    outcome = _run(io)
    assert outcome.exit_code == 3
    assert "not on fork/force/carry" in outcome.failed_line
    assert "restart_gateway" not in io.calls


def test_stamp_mismatch_rolls_back_without_swap():
    io = FakeIO()
    io.stamp = "ffffffffffffffffffffffffffffffffffffffff"
    outcome = _run(io)
    assert outcome.exit_code == 4
    assert "install-stamp.json commit is" in outcome.failed_line
    assert "swap_release" not in io.calls
    assert io.reset_sha == PREV
    assert "restore_release_prev" in io.calls


def test_health_failure_rolls_back():
    io = FakeIO()
    io.health = flu.StepResult(False, "gateway /health is 0, want 200")
    outcome = _run(io)
    assert outcome.exit_code == 4
    assert "/health" in outcome.failed_line
    assert io.reset_sha == PREV


def test_pid_unchanged_rolls_back():
    io = FakeIO()
    io.health = flu.StepResult(False, "gateway.lock pid did not change")
    outcome = _run(io)
    assert outcome.exit_code == 4
    assert "pid did not change" in outcome.failed_line
    assert "git_reset_keep" in io.calls


def test_failed_boot_proof_triggers_rollback():
    log = "2026-11-09 21:00:01,000 [hermes] [bootstrap] first-run setup\n"
    io = FakeIO()
    io.boot = flu.boot_proof(log, NOW)
    outcome = _run(io)
    assert outcome.exit_code == 4
    assert outcome.exit_code != 0
    assert "boot proof failed" in outcome.failed_line
    assert "[bootstrap]" in outcome.failed_line
    assert "restore_release_prev" in io.calls
    assert "git_reset_keep" in io.calls
    assert io.reset_sha == PREV
    assert io.calls.count("restart_gateway") >= 2
    assert io.receipts[-1][1]["rolled_back"] is True
    assert "live-update FAILED" in outcome.summary


def test_boot_proof_rejects_each_bad_marker():
    since = NOW
    samples = [
        "2026-11-09 21:00:01,000 [hermes] [bootstrap] starting",
        "2026-11-09 21:00:01,000 [hermes] couldn't start the gateway",
        "2026-11-09 21:00:01,000 [hermes] first-run setup",
        "2026-11-09 21:00:01,000 [hermes] owner runs old but this Hermes is new",
    ]
    for line in samples:
        result = flu.boot_proof(line + "\n", since)
        assert result.ok is False, line
        assert result.line.startswith("boot proof failed:")


def test_boot_proof_accepts_ready_and_roster_after_launch():
    text = "\n".join([
        "2026-11-09 20:00:00,000 [hermes] [bootstrap] old line ignored",
        "2026-11-09 21:00:01,000 [hermes] [boot] Hermes gateway is ready",
        "2026-11-09 21:00:02,000 [hermes] [bot-roster win=main] snapshot count=2",
    ])
    result = flu.boot_proof(text, NOW)
    assert result.ok is True


def test_boot_proof_ignores_lines_before_launch():
    text = "2026-11-09 20:00:00,000 [hermes] [boot] Hermes gateway is ready\n"
    result = flu.boot_proof(text, NOW)
    assert result.ok is False
    assert "missing [boot] Hermes gateway is ready" in result.line


def test_mymeter_failures_roll_back():
    cases = [
        flu.mymeter_proof("Down", ["10.1.2.3"], True),
        flu.mymeter_proof("Up", ["8.8.8.8"], True),
        flu.mymeter_proof("Up", ["10.1.2.3"], False),
    ]
    for proof in cases:
        io = FakeIO()
        io.meter = proof
        outcome = _run(io)
        assert outcome.exit_code == 4
        assert "MyMeter proof failed" in outcome.failed_line
        assert io.reset_sha == PREV


def test_girl_quiet_requires_turn_ended_for_60s():
    now = NOW
    assert flu.girl_from_text("solara", None, now).ok is False
    assert flu.girl_from_text("solara", "2026-11-09 20:59:30,000 INFO still going\n", now).ok is False
    recent = "2026-11-09 20:59:30,000 INFO Turn ended: session=s\n"
    assert flu.girl_from_text("solara", recent, now).ok is False
    assert flu.girl_from_text("solara", QUIET_LINE + "\n", now).ok is True


def test_dns_command_uses_resolve_dnsname_not_nslookup():
    script = flu.dns_powershell()
    assert "Resolve-DnsName" in script
    assert "nslookup" not in script.lower()
    assert flu.DNS_NAME in script


def test_gateway_restart_argv_is_explorer_plus_vbs():
    argv = flu.gateway_restart_argv(Path(r"C:\hermes\restart-gateway.vbs"))
    assert argv[0] == "explorer.exe"
    assert argv[1].endswith("restart-gateway.vbs")


def test_hidden_kwargs_uses_create_no_window(monkeypatch):
    monkeypatch.setattr(flu.os, "name", "nt")
    flags = flu.hidden_kwargs()["creationflags"]
    assert flags & 0x08000000 == 0x08000000
    assert flu.CREATE_NO_WINDOW == 0x08000000


def test_tcp_open_does_not_log_in():
    import inspect

    source = inspect.getsource(flu.tcp_open)
    assert "create_connection" in source
    assert "pyodbc" not in source
    assert "sqlalchemy" not in source


def test_guard_refuses_kanban_worker_on_live_install(tmp_path):
    live = tmp_path / "hermes-agent"
    live.mkdir()
    line = flu.guard_worker_live(live, False, task="t_d3552f80", live_repo=live)
    assert line is not None
    assert "t_d3552f80" in line
    assert "--allow-live" in line
    assert flu.guard_worker_live(live, True, task="t_d3552f80", live_repo=live) is None
    assert flu.guard_worker_live(live, False, task="", live_repo=live) is None


def test_parser_requires_to_and_accepts_desktop():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_force_live_update_parser(sub, cmd_force_live_update=lambda args: None)
    with pytest.raises(SystemExit):
        parser.parse_args(["force-live-update"])
    args = parser.parse_args(["force-live-update", "--to", TARGET, "--desktop", "--allow-live"])
    assert args.to == TARGET
    assert args.desktop is True
    assert args.allow_live is True


def test_main_wires_the_command():
    text = Path(__file__).resolve().parents[2].joinpath("hermes_cli", "main.py").read_text(encoding="utf-8")
    assert '"force-live-update"' in text
    assert "build_force_live_update_parser" in text
    assert "def cmd_force_live_update" in text


def test_receipt_file_has_summary_and_json(tmp_path):
    path = flu.write_receipt_file(tmp_path, "2026-11-09T210000", "live-update FAILED boot proof failed: x", {"ok": False})
    body = path.read_text(encoding="utf-8")
    assert body.startswith("live-update FAILED")
    assert path.name == "live-update-2026-11-09T210000.log"
    payload = json.loads(body.split("\n", 1)[1])
    assert payload["ok"] is False


def test_config_snapshot_copies_yaml_not_env(tmp_path):
    home = tmp_path / "home"
    (home / "profiles" / "solara").mkdir(parents=True)
    (home / "config.yaml").write_text("root: 1\n", encoding="utf-8")
    (home / ".env").write_text("SECRET=nope\n", encoding="utf-8")
    (home / "profiles" / "solara" / "config.yaml").write_text("p: 1\n", encoding="utf-8")
    (home / "profiles" / "solara" / ".env").write_text("SECRET=nope\n", encoding="utf-8")
    dest = tmp_path / "backups" / "settings" / "2026-11-09T210000-live-update"
    copied = flu.snapshot_config_files(home, dest)
    assert "config.yaml" in copied
    assert "profiles/solara/config.yaml" in copied
    assert not (dest / ".env").exists()
    assert not (dest / "profiles" / "solara" / ".env").exists()


def test_running_cards_query_and_unreadable_board(tmp_path):
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t_run', 'busy', 'running')")
    conn.execute("INSERT INTO tasks VALUES ('t_done', 'finished', 'done')")
    conn.commit()
    conn.close()
    cards = flu.read_running_cards([("default", db), ("broken", tmp_path / "missing.db")])
    ids = [card.task_id for card in cards]
    assert "t_run" in ids
    assert "t_done" not in ids
    assert any(card.task_id == "unreadable" for card in cards)


def test_real_ff_refuses_when_sha_is_not_the_tip(tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        cmd = args[3:]
        stdout = ""
        code = 0
        if cmd[:2] == ["rev-parse", "--verify"]:
            stdout = TARGET + "\n"
        elif cmd[:1] == ["rev-parse"] and cmd[1] == "fork/force/carry":
            stdout = "9999999999999999999999999999999999999999\n"
        elif cmd[:2] == ["merge-base", "--is-ancestor"]:
            code = 1
        return argparse.Namespace(returncode=code, stdout=stdout, stderr="")

    io = flu.RealLiveUpdateIO(tmp_path, tmp_path, run=fake_run, popen=lambda *a, **k: None)
    result = io.ff_only(TARGET)
    assert result.ok is False
    assert "not on fork/force/carry" in result.line
    assert all("creationflags" in kwargs and kwargs["creationflags"] & 0x08000000 for _args, kwargs in calls)
    assert not any("merge" in args for args, _kwargs in calls)


def test_real_ff_calls_hidden_ff_only_merge(tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        cmd = args[3:]
        stdout = TARGET + "\n" if "rev-parse" in cmd else ""
        return argparse.Namespace(returncode=0, stdout=stdout, stderr="")

    io = flu.RealLiveUpdateIO(tmp_path, tmp_path, run=fake_run, popen=lambda *a, **k: None)
    result = io.ff_only("abc123")
    assert result.ok is True
    assert result.sha == TARGET
    merge = [args for args in calls if "merge" in args]
    assert merge
    assert "--ff-only" in merge[0]
    assert "fork/force/carry" in merge[0]


def test_real_gateway_restart_uses_explorer_and_hides_the_window(tmp_path):
    vbs = tmp_path / "restart-gateway.vbs"
    vbs.write_text("'", encoding="utf-8")
    launched = []

    def fake_popen(args, **kwargs):
        launched.append((list(args), kwargs))

    io = flu.RealLiveUpdateIO(tmp_path, tmp_path, popen=fake_popen)
    result = io.restart_gateway()
    assert result.ok is True
    argv, kwargs = launched[0]
    assert argv[0] == "explorer.exe"
    assert argv[1] == str(vbs)
    assert kwargs["creationflags"] & 0x08000000


def test_real_boot_wait_uses_the_parser_and_does_not_sleep_past_budget(tmp_path):
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "desktop.log").write_text(
        "2026-11-09 21:00:01,000 [hermes] couldn't start\n",
        encoding="utf-8",
    )
    clock = {"now": NOW}

    def now():
        return clock["now"]

    def sleep(_seconds):
        clock["now"] = clock["now"] + timedelta(seconds=BOOT_JUMP)

    BOOT_JUMP = flu.BOOT_BUDGET_SECONDS
    io = flu.RealLiveUpdateIO(tmp_path, home, sleep=sleep, now=now)
    result = io.wait_boot(NOW)
    assert result.ok is False
    assert "couldn't start" in result.line


def test_reprove_failure_is_named_and_exit_stays_nonzero():
    io = FakeIO()
    io.boot = flu.StepResult(False, "boot proof failed: missing [boot] Hermes gateway is ready")
    io.boot_after = flu.StepResult(False, "boot proof failed: missing bot-roster snapshot")
    outcome = _run(io)
    assert outcome.exit_code == 4
    assert "missing [boot] Hermes gateway is ready" in outcome.failed_line
    assert any("re-prove failed" in note for note in outcome.notes)
