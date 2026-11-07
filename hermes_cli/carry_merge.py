"""Merge the fork's force/carry into this install and rebuild.

The live install is the directory that contains this module, or an absolute
``--repo`` path. The process cwd is never the target. origin is NousResearch
upstream and is not fetched. This does not switch branch. A failed step
returns non-zero and does not claim success.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

FORK_OWNER = "HudsonHawk7"
FORK_REPO = "hermes-agent"
FORK_REF = "force/carry"


class CarryMergeError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code if exit_code else 1


def install_root() -> Path:
    """The checkout that contains this module. Not the process cwd."""
    return Path(__file__).resolve().parents[1]


def _redact(text: str) -> str:
    return re.sub(r"://[^/\s@]+@", "://***@", text or "")


def command_text(argv: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


def git_argv(repo: Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo), *args]


def resolve_repo(explicit: str | None) -> Path:
    text = (explicit or "").strip()
    if not text:
        repo = install_root()
    else:
        raw = Path(text).expanduser()
        if not raw.is_absolute():
            raise CarryMergeError(
                "--repo must be an absolute path, not a cwd-relative one: "
                + text
            )
        repo = raw.resolve()
    if not (repo / ".git").exists():
        raise CarryMergeError(f"Not a git checkout: {repo}")
    return repo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        git_argv(repo, *args),
        capture_output=True,
        text=True,
        check=False,
    )


def current_branch(repo: Path) -> str:
    proc = _git(repo, "branch", "--show-current")
    branch = (proc.stdout or "").strip()
    if proc.returncode != 0 or not branch:
        detail = _redact((proc.stderr or proc.stdout or "").strip())
        raise CarryMergeError(
            f"Could not read the branch at {repo}. {detail}".strip()
        )
    return branch


def fork_remote(repo: Path) -> str:
    proc = _git(repo, "remote", "-v")
    if proc.returncode != 0:
        detail = _redact((proc.stderr or "").strip())
        raise CarryMergeError(f"Could not list remotes at {repo}. {detail}")
    slug = f"{FORK_OWNER}/{FORK_REPO}".lower()
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and slug in parts[1].lower():
            names.append(parts[0])
    if not names:
        raise CarryMergeError(
            f"No remote at {repo} points at {FORK_OWNER}/{FORK_REPO}. "
            "Refusing to fetch the upstream remote."
        )
    for name in names:
        if name == "fork":
            return name
    return names[0]


def enabled_plugin_names() -> list[str]:
    """The live plugins.enabled list. Missing means none. A bad shape fails loud."""
    from hermes_cli.config import load_config_readonly

    cfg = load_config_readonly()
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if enabled is None:
        return []
    if not isinstance(enabled, list):
        raise CarryMergeError("plugins.enabled is not a list. Refusing to merge.")
    return [str(item) for item in enabled]


def planned_commands(repo: Path, remote: str) -> list[str]:
    return [
        command_text(git_argv(repo, "fetch", remote, FORK_REF)),
        command_text(git_argv(repo, "merge", "--no-edit", f"{remote}/{FORK_REF}")),
        command_text([sys.executable, "-m", "pm.cli", "install"]),
    ]


def plan(explicit: str | None = None) -> dict[str, object]:
    repo = resolve_repo(explicit)
    branch = current_branch(repo)
    if branch != FORK_REF:
        raise CarryMergeError(
            f"{repo} is on {branch}, not {FORK_REF}. Refusing to switch branch."
        )
    remote = fork_remote(repo)
    return {
        "repo": str(repo),
        "remote": remote,
        "branch": branch,
        "commands": planned_commands(repo, remote),
        "checks_out_another_branch": False,
    }


def _emit(text: str, *, failed: bool) -> None:
    cleaned = _redact(text).strip()
    if cleaned:
        print(cleaned, file=sys.stderr if failed else sys.stdout)


def _run_step(repo: Path, *args: str) -> None:
    argv = git_argv(repo, *args)
    print("$ " + command_text(argv))
    proc = _git(repo, *args)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        _emit(output, failed=True)
        raise CarryMergeError(
            f"FAILED: {command_text(argv)} exited {proc.returncode}.",
            proc.returncode or 1,
        )
    _emit(output, failed=False)


def _rebuild(repo: Path) -> None:
    argv = [sys.executable, "-m", "pm.cli", "install"]
    print(f"$ {command_text(argv)} (cwd {repo})")
    proc = subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        _emit(output, failed=True)
        raise CarryMergeError(
            f"FAILED: rebuild exited {proc.returncode} in {repo}.",
            proc.returncode or 1,
        )
    _emit(output, failed=False)


def run_carry_merge(args: object | None = None) -> int:
    raw = getattr(args, "repo", None) if args is not None else None
    explicit = raw if isinstance(raw, str) or raw is None else str(raw)
    try:
        info = plan(explicit)
        repo = Path(str(info["repo"]))
        remote = str(info["remote"])
        names = enabled_plugin_names()
        shown = ", ".join(names) if names else "none"
        print(f"plugins.enabled: {shown}")
        _run_step(repo, "fetch", remote, FORK_REF)
        _run_step(repo, "merge", "--no-edit", f"{remote}/{FORK_REF}")
        _rebuild(repo)
        print(f"Merged {remote}/{FORK_REF} into {repo}. Every step exited 0.")
        _log.info("Force carry merge finished for %s", repo)
        return 0
    except CarryMergeError as exc:
        print(str(exc), file=sys.stderr)
        _log.error("%s", exc)
        return exc.exit_code if exc.exit_code else 1


def build_carry_parser(
    subparsers: argparse._SubParsersAction,
    *,
    cmd_carry_merge: Callable[..., object],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "carry",
        help="Merge the fork's force/carry into this install and rebuild",
    )
    nested = parser.add_subparsers(dest="carry_command", required=True)
    merge = nested.add_parser(
        "merge",
        help="Merge HudsonHawk7/hermes-agent force/carry into this install",
    )
    merge.add_argument(
        "--repo",
        default=None,
        help="Absolute path of the live install. Default: this install.",
    )
    merge.set_defaults(func=cmd_carry_merge)
    return parser
