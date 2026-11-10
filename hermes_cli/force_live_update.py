"""One command for the HermesForce live-update protocol (steps 2-9).

``hermes force-live-update --to <merged sha> [--desktop]`` is the only door
that fast-forwards, packs, swaps, or restarts the live install. Builders do
not run it against that install. Architect_ClaudeCode runs it after a
JocastaNu PASS.

Every side effect sits behind ``LiveUpdateIO``. Tests pass a fake. The real
adapter uses ``CREATE_NO_WINDOW`` and never opens a console. A kanban worker
that targets the live install must pass ``--allow-live``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

CREATE_NO_WINDOW = 0x08000000
GIRLS = ("solara", "aura")
QUIET_SECONDS = 60
BOOT_BUDGET_SECONDS = 300
HEALTH_BUDGET_SECONDS = 90
DNS_NAME = "sql-prod.corp.mymeter.ai"
SQL_PORT = 1433
SHORTCUT_NAME = "Hermes - MyMeter SQL.lnk"
READY_LINE = "[boot] Hermes gateway is ready"
BAD_BOOT = ("[bootstrap]", "couldn't start", "first-run setup")
OWNER_MISMATCH = re.compile(r"owner runs .+ but this Hermes is")
LOG_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


@dataclass
class RunningCard:
    board: str
    task_id: str
    title: str


@dataclass
class GirlState:
    name: str
    ok: bool
    detail: str


@dataclass
class StepResult:
    ok: bool
    line: str
    sha: str = ""


@dataclass
class LiveUpdateOutcome:
    exit_code: int
    failed_line: str | None
    summary: str
    receipt_path: str | None
    rolled_back: bool
    notes: list[str] = field(default_factory=list)


def parse_stamp(line: str) -> datetime | None:
    match = LOG_STAMP.match(line)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    return stamp.replace(microsecond=int(match.group(2)) * 1000)


def girl_from_text(name: str, text: str | None, now: datetime) -> GirlState:
    """A girl is quiet only when her log ends in Turn ended, 60s or older."""
    if text is None:
        return GirlState(name, False, "agent log missing")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return GirlState(name, False, "agent log empty")
    last = lines[-1]
    if "Turn ended" not in last:
        return GirlState(name, False, f"last line is not Turn ended: {last[:160]}")
    stamp = parse_stamp(last)
    if stamp is None:
        return GirlState(name, False, "Turn ended line has no timestamp")
    age = (now - stamp).total_seconds()
    if age < QUIET_SECONDS:
        return GirlState(name, False, f"Turn ended {age:.0f}s ago (need 60s+)")
    return GirlState(name, True, f"quiet {age:.0f}s")


def evaluate_gate(cards: list[RunningCard], girls: list[GirlState]) -> StepResult:
    lines: list[str] = []
    for card in cards:
        lines.append(f"running: board={card.board} id={card.task_id} title={card.title}")
    for girl in girls:
        if not girl.ok:
            lines.append(f"{girl.name}: {girl.detail}")
    if lines:
        return StepResult(False, "refused: quiet window is not clear\n" + "\n".join(lines))
    return StepResult(True, "quiet window clear")


def boot_proof(text: str, since: datetime) -> StepResult:
    """Proof from desktop.log lines at or after the relaunch time."""
    ready = False
    roster = False
    bad: str | None = None
    for line in text.splitlines():
        stamp = parse_stamp(line)
        if stamp is None or stamp < since:
            continue
        for marker in BAD_BOOT:
            if marker in line:
                bad = marker
                break
        if OWNER_MISMATCH.search(line):
            bad = "owner runs X but this Hermes is Y"
        if READY_LINE in line:
            ready = True
        if "bot-roster" in line and "snapshot" in line:
            roster = True
    if bad:
        return StepResult(False, f"boot proof failed: {bad}")
    if not ready:
        return StepResult(False, f"boot proof failed: missing {READY_LINE}")
    if not roster:
        return StepResult(False, "boot proof failed: missing bot-roster snapshot")
    return StepResult(True, "boot proof ok")


def mymeter_proof(adapter_status: str | None, addresses: list[str], tcp_ok: bool) -> StepResult:
    status = (adapter_status or "").strip()
    if status.lower() != "up":
        shown = status or "missing"
        return StepResult(False, f"MyMeter proof failed: Data Channel Offload adapter {shown}")
    tens = [addr for addr in addresses if addr.startswith("10.")]
    if not tens:
        shown = ", ".join(addresses) if addresses else "no address"
        return StepResult(
            False,
            f"MyMeter proof failed: {DNS_NAME} did not resolve to 10.x ({shown})",
        )
    if not tcp_ok:
        return StepResult(False, f"MyMeter proof failed: TCP {SQL_PORT} not reachable on {tens[0]}")
    return StepResult(True, f"MyMeter proof ok {tens[0]}")


def stamp_matches(stamp_commit: str, target: str) -> StepResult:
    left = (stamp_commit or "").strip().lower()
    right = (target or "").strip().lower()
    if not left:
        return StepResult(False, "install-stamp.json commit is missing")
    if left != right:
        return StepResult(False, f"install-stamp.json commit is {stamp_commit} not {target}")
    return StepResult(True, "install-stamp.json commit matches")


def dns_powershell(name: str = DNS_NAME) -> str:
    """Resolve with Resolve-DnsName. Never nslookup."""
    return (
        f"Resolve-DnsName -Name {name} -Type A -ErrorAction Stop "
        "| Select-Object -ExpandProperty IPAddress"
    )


def adapter_powershell() -> str:
    return (
        "Get-NetAdapter | Where-Object { "
        "$_.InterfaceDescription -like '*Data Channel Offload*' "
        "-or $_.Name -like '*Data Channel Offload*' "
        "} | Select-Object -ExpandProperty Status"
    )


def gateway_restart_argv(vbs: Path) -> list[str]:
    """The same hidden path restart-gateway.vbs uses, in the GM's session."""
    return ["explorer.exe", str(vbs)]


def shortcut_script(lnk: Path) -> str:
    quoted = str(lnk).replace("'", "''")
    return (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{quoted}'); "
        "Write-Output $s.TargetPath; Write-Output '---'; "
        "Write-Output $s.Arguments; Write-Output '---'; "
        "Write-Output $s.WorkingDirectory"
    )


def hidden_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def poll_until(predicate, timeout_s: float, sleep: Callable[[float], None], now: Callable[[], datetime]):
    start = now()
    last = (False, "not ready")
    while True:
        last = predicate()
        if last[0]:
            return last
        if (now() - start).total_seconds() >= timeout_s:
            return last
        sleep(1.0)


def live_install_repo() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        return Path(local) / "hermes" / "hermes-agent"
    return Path.home() / ".hermes" / "hermes-agent"


def live_install_home() -> Path:
    return live_install_repo().parent


def guard_worker_live(repo: Path, allow_live: bool, *, task: str, live_repo: Path) -> str | None:
    """Refuse a kanban worker that targets the live install without --allow-live."""
    if not (task or "").strip():
        return None
    try:
        same = repo.resolve() == live_repo.resolve()
    except OSError:
        same = os.path.normcase(str(repo)) == os.path.normcase(str(live_repo))
    if same and not allow_live:
        return (
            f"refused: kanban worker {task.strip()} may not live-update the live install "
            "(pass --allow-live only as Architect_ClaudeCode after a JocastaNu PASS)"
        )
    return None


def discover_board_dbs(home: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    default = home / "kanban.db"
    if default.is_file():
        found.append(("default", default))
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        for child in sorted(boards.iterdir()):
            db = child / "kanban.db"
            if child.is_dir() and db.is_file():
                found.append((child.name, db))
    return found


def query_running(board: str, path: Path) -> list[RunningCard]:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        rows = conn.execute("SELECT id, title FROM tasks WHERE status = 'running'").fetchall()
    finally:
        conn.close()
    return [RunningCard(board, str(row[0]), str(row[1] or "")) for row in rows]


def read_running_cards(boards: list[tuple[str, Path]]) -> list[RunningCard]:
    cards: list[RunningCard] = []
    for name, path in boards:
        try:
            cards.extend(query_running(name, path))
        except Exception as exc:
            cards.append(RunningCard(name, "unreadable", f"could not read board: {exc}"))
    return cards


def snapshot_config_files(home: Path, dest: Path) -> list[str]:
    """Copy root and profile config.yaml files. Never copy .env contents into a log."""
    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    root = home / "config.yaml"
    if root.is_file():
        shutil.copy2(root, dest / "config.yaml")
        copied.append("config.yaml")
    profiles = home / "profiles"
    if profiles.is_dir():
        for child in sorted(p for p in profiles.iterdir() if p.is_dir()):
            cfg = child / "config.yaml"
            if not cfg.is_file():
                continue
            target = dest / "profiles" / child.name / "config.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg, target)
            copied.append(f"profiles/{child.name}/config.yaml")
    return copied


def write_receipt_file(directory: Path, stamp: str, summary: str, receipt: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"live-update-{stamp}.log"
    body = summary + "\n" + json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def read_gateway_pid(path: Path) -> int | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    pid = data.get("pid")
    if pid is None:
        return None
    return int(pid)


def tcp_open(host: str, port: int = SQL_PORT, timeout: float = 5.0) -> bool:
    """Reachability only. No SQL login."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_protocol(io, *, target_sha: str, desktop: bool) -> LiveUpdateOutcome:
    """Steps 2-9. ``io`` is a fake in tests and RealLiveUpdateIO on the live run."""
    now = io.now()
    stamp = now.strftime("%Y-%m-%dT%H%M%S")
    gate = evaluate_gate(io.running_cards(), io.girl_states(now))
    if not gate.ok:
        return _finish(io, stamp, exit_code=2, failed_line=gate.line, rolled_back=False, target_sha=target_sha, desktop=desktop, prev="")

    prev = io.head()
    io.snapshot_release_prev()
    io.snapshot_configs(stamp)
    ff = io.ff_only(target_sha)
    if not ff.ok:
        return _finish(
            io, stamp, exit_code=3, failed_line=ff.line, rolled_back=False,
            target_sha=target_sha, desktop=desktop, prev=prev,
        )
    landed = ff.sha or target_sha
    swapped = False
    if desktop:
        pack = io.pack_desktop(landed)
        if not pack.ok:
            return _rollback(io, prev, pack.line, stamp, target_sha, desktop, swapped)
        match = stamp_matches(io.stamp_commit(), landed)
        if not match.ok:
            return _rollback(io, prev, match.line, stamp, target_sha, desktop, swapped)
        io.swap_release()
        swapped = True

    old_pid = io.gateway_pid()
    restart = io.restart_gateway()
    if not restart.ok:
        return _rollback(io, prev, restart.line, stamp, target_sha, desktop, swapped)
    health = io.wait_gateway(old_pid)
    if not health.ok:
        return _rollback(io, prev, health.line, stamp, target_sha, desktop, swapped)
    launch, launched = io.launch_desktop()
    if not launched.ok:
        return _rollback(io, prev, launched.line, stamp, target_sha, desktop, swapped)
    boot = io.wait_boot(launch)
    if not boot.ok:
        return _rollback(io, prev, boot.line, stamp, target_sha, desktop, swapped)
    meter = io.mymeter()
    if not meter.ok:
        return _rollback(io, prev, meter.line, stamp, target_sha, desktop, swapped)

    summary = f"live-update ok sha={landed} desktop={'yes' if desktop else 'no'}"
    return _finish(
        io, stamp, exit_code=0, failed_line=None, rolled_back=False,
        target_sha=landed, desktop=desktop, prev=prev, summary=summary,
    )


def _rollback(io, prev: str, failed_line: str, stamp: str, target_sha: str, desktop: bool, swapped: bool) -> LiveUpdateOutcome:
    notes = [failed_line]
    try:
        if swapped or desktop:
            io.restore_release_prev()
    except Exception as exc:
        notes.append(f"rollback restore failed: {exc}")
    try:
        io.git_reset_keep(prev)
    except Exception as exc:
        notes.append(f"rollback git reset failed: {exc}")
    try:
        io.restart_gateway()
        launch, launched = io.launch_desktop()
        if not launched.ok:
            notes.append(f"re-prove launch failed: {launched.line}")
        else:
            reprove = io.wait_boot(launch)
            if not reprove.ok:
                notes.append(f"re-prove failed: {reprove.line}")
    except Exception as exc:
        notes.append(f"rollback restart failed: {exc}")
    return _finish(
        io, stamp, exit_code=4, failed_line=failed_line, rolled_back=True,
        target_sha=target_sha, desktop=desktop, prev=prev, notes=notes,
    )


def _finish(
    io,
    stamp: str,
    *,
    exit_code: int,
    failed_line: str | None,
    rolled_back: bool,
    target_sha: str,
    desktop: bool,
    prev: str,
    summary: str | None = None,
    notes: list[str] | None = None,
) -> LiveUpdateOutcome:
    if summary is None:
        summary = f"live-update FAILED {failed_line}" if failed_line else "live-update FAILED"
    receipt = {
        "desktop": desktop,
        "failed_line": failed_line,
        "notes": notes or ([failed_line] if failed_line else []),
        "ok": exit_code == 0,
        "prev_sha": prev,
        "rolled_back": rolled_back,
        "summary": summary.split("\n", 1)[0],
        "target_sha": target_sha,
        "ts": stamp,
    }
    path = str(io.write_receipt(stamp, summary, receipt))
    return LiveUpdateOutcome(exit_code, failed_line, summary, path, rolled_back, notes or [])


def hidden_run(args, **kwargs):
    if os.name == "nt":
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | CREATE_NO_WINDOW
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    check = kwargs.pop("check", False)
    return subprocess.run(args, check=check, **kwargs)


def hidden_popen(args, **kwargs):
    if os.name == "nt":
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | CREATE_NO_WINDOW
    return subprocess.Popen(args, **kwargs)


class RealLiveUpdateIO:
    """The live adapter. Tests construct it on a temp root, or replace ``run``."""

    def __init__(self, repo: Path, home: Path, *, sleep=time.sleep, now=None, run=None, popen=None):
        self.repo = Path(repo)
        self.home = Path(home)
        self.sleep = sleep
        self._now = now or datetime.now
        self.run = run or hidden_run
        self.popen = popen or hidden_popen
        self._staging: Path | None = None
        self._resolved = ""

    def _hidden(self) -> dict:
        if os.name == "nt":
            return {"creationflags": CREATE_NO_WINDOW}
        return {}

    def now(self) -> datetime:
        return self._now()

    def running_cards(self) -> list[RunningCard]:
        return read_running_cards(discover_board_dbs(self.home))

    def girl_states(self, now: datetime) -> list[GirlState]:
        states = []
        for name in GIRLS:
            path = self.home / "profiles" / name / "logs" / "agent.log"
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            states.append(girl_from_text(name, text, now))
        return states

    def head(self) -> str:
        proc = self._git("rev-parse", "HEAD")
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "git rev-parse HEAD failed").strip())
        return proc.stdout.strip()

    def snapshot_release_prev(self) -> str:
        src = self.repo / "apps" / "desktop" / "release" / "win-unpacked"
        dest_root = self.repo / "apps" / "desktop" / "release-prev"
        dest = dest_root / "win-unpacked"
        if not src.is_dir():
            return "no win-unpacked to snapshot"
        if dest_root.exists():
            shutil.rmtree(dest_root)
        shutil.copytree(src, dest)
        return str(dest)

    def snapshot_configs(self, stamp: str) -> str:
        dest = self.home / "backups" / "settings" / f"{stamp}-live-update"
        snapshot_config_files(self.home, dest)
        return str(dest)

    def ff_only(self, sha: str) -> StepResult:
        fetch = self._git("fetch", "fork", "force/carry")
        if fetch.returncode != 0:
            return StepResult(False, "refused: git fetch fork force/carry failed")
        want = self._git("rev-parse", "--verify", sha)
        if want.returncode != 0:
            return StepResult(False, f"refused: {sha} is not a commit")
        full = want.stdout.strip()
        tip = self._git("rev-parse", "fork/force/carry")
        if tip.returncode != 0:
            return StepResult(False, "refused: fork/force/carry is not available")
        tip_sha = tip.stdout.strip()
        if tip_sha != full:
            ancestor = self._git("merge-base", "--is-ancestor", full, "fork/force/carry")
            if ancestor.returncode != 0:
                return StepResult(False, f"refused: {sha} is not on fork/force/carry")
            return StepResult(False, f"refused: fork/force/carry is {tip_sha} not {full}")
        merge = self._git("merge", "--ff-only", "fork/force/carry")
        if merge.returncode != 0:
            detail = (merge.stderr or merge.stdout or "not a fast-forward").strip()
            return StepResult(False, f"refused: not a fast-forward ({detail})")
        self._resolved = full
        return StepResult(True, f"fast-forwarded to {full}", sha=full)

    def pack_desktop(self, sha: str) -> StepResult:
        stamp = self.now().strftime("%Y-%m-%dT%H%M%S")
        staging = self.repo / "apps" / "desktop" / f"release-live-update-{stamp}"
        staging.mkdir(parents=True, exist_ok=True)
        desktop = self.repo / "apps" / "desktop"
        npm = "npm.cmd" if os.name == "nt" else "npm"
        build = self.run([npm, "run", "build"], cwd=str(desktop), **self._hidden())
        if build.returncode != 0:
            return StepResult(False, "desktop pack failed: npm run build")
        packed = self.run(
            [npm, "run", "builder", "--", "--dir", "--publish", "never", f"-c.directories.output={staging}"],
            cwd=str(desktop),
            **self._hidden(),
        )
        if packed.returncode != 0:
            return StepResult(False, "desktop pack failed: npm run builder --dir")
        self._staging = staging
        return StepResult(True, f"packed {sha} to {staging}")

    def stamp_commit(self) -> str:
        if self._staging is None:
            return ""
        path = self._staging / "win-unpacked" / "resources" / "install-stamp.json"
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        commit = str(data.get("commit") or "")
        if not commit:
            return ""
        resolved = self._git("rev-parse", "--verify", commit)
        if resolved.returncode != 0:
            return commit
        return resolved.stdout.strip()

    def swap_release(self) -> None:
        if self._staging is None:
            raise RuntimeError("no staged desktop to swap")
        staged = self._staging / "win-unpacked"
        live = self.repo / "apps" / "desktop" / "release" / "win-unpacked"
        if live.exists():
            shutil.rmtree(live)
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, live)

    def restore_release_prev(self) -> None:
        prev = self.repo / "apps" / "desktop" / "release-prev" / "win-unpacked"
        live = self.repo / "apps" / "desktop" / "release" / "win-unpacked"
        if not prev.is_dir():
            return
        if live.exists():
            shutil.rmtree(live)
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(prev, live)

    def git_reset_keep(self, sha: str) -> None:
        proc = self._git("reset", "--keep", sha)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "git reset --keep failed").strip())

    def gateway_pid(self) -> int | None:
        return read_gateway_pid(self.home / "gateway.lock")

    def restart_gateway(self) -> StepResult:
        vbs = self.home / "restart-gateway.vbs"
        if not vbs.is_file():
            return StepResult(False, "restart-gateway.vbs is missing")
        self.popen(gateway_restart_argv(vbs), **self._hidden())
        return StepResult(True, "gateway restart launched")

    def wait_gateway(self, old_pid: int | None) -> StepResult:
        def ready():
            code = health_status(self.health_url())
            pid = self.gateway_pid()
            if code == 200 and pid is not None and pid != old_pid:
                return (True, f"gateway health 200 pid {pid}")
            if code != 200:
                return (False, f"gateway /health is {code}, want 200")
            return (False, "gateway.lock pid did not change")

        ok, line = poll_until(ready, HEALTH_BUDGET_SECONDS, self.sleep, self.now)
        return StepResult(ok, line)

    def launch_desktop(self) -> tuple[datetime, StepResult]:
        appdata = os.environ.get("APPDATA", "")
        lnk = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME
        if not lnk.is_file():
            return self.now(), StepResult(False, f"shortcut missing: {SHORTCUT_NAME}")
        proc = self.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", shortcut_script(lnk)],
            **self._hidden(),
        )
        if proc.returncode != 0:
            return self.now(), StepResult(False, "could not read the Hermes - MyMeter SQL shortcut")
        parts = (proc.stdout or "").split("---")
        target = parts[0].strip() if parts else ""
        arguments = parts[1].strip() if len(parts) > 1 else ""
        workdir = parts[2].strip() if len(parts) > 2 else ""
        if not target:
            return self.now(), StepResult(False, "Hermes - MyMeter SQL shortcut has no target")
        argv = [target, *arguments.split()] if arguments else [target]
        launch = self.now()
        self.popen(argv, cwd=workdir or None, **self._hidden())
        return launch, StepResult(True, f"launched {target}")

    def wait_boot(self, launch: datetime) -> StepResult:
        log_path = self.home / "logs" / "desktop.log"

        def ready():
            if not log_path.is_file():
                return (False, f"boot proof failed: missing {READY_LINE}")
            text = log_path.read_text(encoding="utf-8", errors="replace")
            result = boot_proof(text, launch)
            return (result.ok, result.line)

        ok, line = poll_until(ready, BOOT_BUDGET_SECONDS, self.sleep, self.now)
        return StepResult(ok, line)

    def mymeter(self) -> StepResult:
        adapter = self.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", adapter_powershell()],
            **self._hidden(),
        )
        status = (adapter.stdout or "").strip().splitlines()
        adapter_status = status[-1].strip() if status else ""
        dns = self.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", dns_powershell()],
            **self._hidden(),
        )
        addresses = [line.strip() for line in (dns.stdout or "").splitlines() if line.strip()]
        tens = [addr for addr in addresses if addr.startswith("10.")]
        tcp_ok = tcp_open(tens[0]) if tens else False
        return mymeter_proof(adapter_status, addresses, tcp_ok)

    def write_receipt(self, stamp: str, summary: str, receipt: dict) -> Path:
        return write_receipt_file(self.home / "logs", stamp, summary, receipt)

    def health_url(self) -> str:
        port = os.environ.get("API_SERVER_PORT") or os.environ.get("HERMES_GATEWAY_PORT") or "8642"
        if not str(port).isdigit():
            port = "8642"
        return f"http://127.0.0.1:{port}/health"

    def _git(self, *args: str):
        return self.run(["git", "-C", str(self.repo), *args], **self._hidden())


def health_status(url: str, timeout: float = 2.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return 0


def main(args) -> int:
    """CLI entry. Refuses a kanban worker on the live install unless --allow-live."""
    repo = live_install_repo()
    home = live_install_home()
    refusal = guard_worker_live(
        repo,
        bool(getattr(args, "allow_live", False)),
        task=os.environ.get("HERMES_KANBAN_TASK", ""),
        live_repo=repo,
    )
    if refusal:
        print(refusal)
        return 2
    io = RealLiveUpdateIO(repo, home)
    outcome = run_protocol(io, target_sha=args.to, desktop=bool(args.desktop))
    print(outcome.summary)
    if outcome.failed_line and outcome.failed_line not in outcome.summary:
        print(outcome.failed_line)
    return outcome.exit_code
