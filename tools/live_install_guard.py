"""Refuse live HermesForce mutations unless this profile is the install owner.

The check sits on the approval floor (beside approvals.deny) so yolo,
mode=off, and a container skip cannot bypass it. A lane that needs the land
files a card for the install owner instead of doing it. See the
hermesforce-build-protocol skill.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

INSTALL_OWNER_DEFAULT = "Architect_ClaudeCode"
SKILL_NAME = "hermesforce-build-protocol"
REFUSAL = (
    "Only the install owner changes the live HermesForce install. "
    "Load the hermesforce-build-protocol skill and file a card for "
    "Architect_ClaudeCode to land it."
)

_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||[|;&])\s*")
_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(\"[^\"]+\"|\S+)")
_WRAPPER_FLAGS = {
    "powershell": ("-command", "-c", "/c"),
    "powershell.exe": ("-command", "-c", "/c"),
    "pwsh": ("-command", "-c"),
    "pwsh.exe": ("-command", "-c"),
    "cmd": ("/c", "/k"),
    "cmd.exe": ("/c", "/k"),
}
_STOP_VERBS = frozenset({'taskkill', 'stop-process', 'kill', 'pkill', 'killall', 'wmic'})
_MOVE_VERBS = frozenset({
    "mv", "move", "move-item", "ren", "rename", "rename-item",
})
_WRITE_VERBS = frozenset({
    "set-content", "out-file", "add-content", "tee", "cp", "copy", "copy-item",
    "scp", "new-item", "ni", "mkdir", "md", "rm", "del", "erase", "remove-item",
    "ri", "rmdir", "rd",
}) | _MOVE_VERBS
_READ_VERBS = frozenset({
    "rg", "grep", "findstr", "type", "cat", "get-content", "gc", "ls", "dir",
    "get-childitem", "gci", "test-path", "echo", "write-output", "select-string",
})
_GIT_WRITE = frozenset({
    "add", "commit", "checkout", "switch", "reset", "merge", "rebase", "pull",
    "push", "clean", "stash", "am", "cherry-pick", "revert", "mv", "rm",
})
_NPM_VERBS = frozenset({"npm", "npm.cmd", "npm.exe"})
_NPM_MUTATIONS = frozenset({"ci", "install"})
_HERMES_IMAGES = frozenset({"hermes.exe", "hermes"})
_GATEWAY_LIFECYCLE = frozenset({"stop", "restart"})
_RESTART_SCRIPTS = frozenset({"restart-gateway.vbs", "restart-gateway.ps1"})
_VALUE_FLAGS = frozenset({
    "-c", "--prefix", "-path", "-destination", "-literalpath",
})


@dataclass(frozen=True)
class LiveTree:
    install: Path
    release_dirs: tuple[Path, ...]
    write_dirs: tuple[Path, ...]


def block_result() -> dict:
    """Approval-floor payload. approved is false so yolo cannot override it."""
    return {"approved": False, "status": "blocked", "message": REFUSAL}


def install_owner_name(config: Mapping | None = None) -> str:
    """Owner profile from hermesforce.install_owner, else the built-in default.

    An explicit null means nobody is exempt. A missing section keeps the default
    so the guard works before the setting is applied.
    """
    if config is None:
        config = _load_config()
    section = config.get("hermesforce") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping) or "install_owner" not in section:
        return INSTALL_OWNER_DEFAULT
    value = section.get("install_owner")
    if value is None:
        return ""
    return str(value).strip()


def live_tree(hermes_root: Path) -> LiveTree:
    """Protected dirs under the Hermes root (not a profile home)."""
    install = hermes_root / "hermes-agent"
    release = (
        install / "release",
        install / "apps" / "desktop" / "release",
    )
    return LiveTree(
        install=install,
        release_dirs=release,
        write_dirs=(install, hermes_root / "plugins", hermes_root / "desktop-plugins"),
    )


def live_install_block(
    command: str,
    *,
    cwd: str | None = None,
    profile: str | None = None,
    owner: str | None = None,
    hermes_root: Path | None = None,
) -> dict | None:
    """Block a live-install mutation from anyone but the install owner."""
    if not command or not command.strip():
        return None
    profile = current_worker_profile() if profile is None else profile
    owner = install_owner_name() if owner is None else owner
    if _is_owner(profile, owner):
        return None
    root = hermes_root if hermes_root is not None else _hermes_root()
    effective_cwd = cwd if cwd is not None else os.getcwd()
    if _command_mutates(command, effective_cwd, live_tree(root)):
        logger.warning("Live install block for profile %s: %s", profile, command[:200])
        return block_result()
    return None


def write_path_block_message(
    path: str,
    *,
    profile: str | None = None,
    owner: str | None = None,
    hermes_root: Path | None = None,
) -> str | None:
    """Protocol refusal when a file-tool write targets the live install, else None."""
    if not path or not str(path).strip():
        return None
    profile = current_worker_profile() if profile is None else profile
    owner = install_owner_name() if owner is None else owner
    if _is_owner(profile, owner):
        return None
    root = hermes_root if hermes_root is not None else _hermes_root()
    resolved = _resolve(str(path), os.getcwd())
    if resolved is not None and _under_any(resolved, live_tree(root).write_dirs):
        return REFUSAL
    return None


def current_worker_profile() -> str:
    """Profile this process is acting as. Kanban pins HERMES_PROFILE."""
    try:
        from hermes_cli.profiles import current_profile_name
        name = current_profile_name(default="")
    except Exception:
        name = os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_PROFILE_NAME") or ""
    return (name or "").strip()


def _load_config() -> Mapping:
    try:
        from hermes_cli.config import load_config_readonly
        loaded = load_config_readonly()
    except Exception:
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def _hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def _is_owner(profile: str, owner: str) -> bool:
    return bool(owner) and profile.casefold() == owner.casefold()


def _command_mutates(command: str, cwd: str, tree: LiveTree) -> bool:
    if _stops_gateway(command) or _runs_restart_script(command):
        return True
    for segment in _segments(command):
        tokens = _unwrap(_split_command(segment))
        if _segment_mutates(segment, tokens, cwd, tree):
            return True
    return False


def _segment_mutates(segment: str, tokens: list[str], cwd: str, tree: LiveTree) -> bool:
    return (
        _stops_hermes_exe(tokens)
        or _npm_mutates_install(tokens, cwd, tree.install)
        or _moves_under(tokens, cwd, tree.release_dirs)
        or _writes_protected(segment, tokens, cwd, tree.write_dirs)
    )


def _segments(command: str) -> list[str]:
    return [part for part in _SEGMENT_SPLIT.split(command) if part.strip()]


def _split_command(command: str) -> list[str]:
    try:
        raw = shlex.split(command, posix=False)
    except ValueError:
        raw = command.split()
    return [token.strip("\"") for token in raw if token.strip("\"")]


def _unwrap(tokens: list[str]) -> list[str]:
    if not tokens:
        return tokens
    flags = _WRAPPER_FLAGS.get(_verb(tokens))
    if not flags:
        return tokens
    for index, token in enumerate(tokens[1:], 1):
        if token.lower() in flags and index + 1 < len(tokens):
            return _unwrap(_split_command(" ".join(tokens[index + 1 :])))
    return tokens


def _verb(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return _basename(tokens[0]).lower()


def _basename(token: str) -> str:
    return token.replace('\\', "/").rsplit("/", 1)[-1]


def _stops_hermes_exe(tokens: list[str]) -> bool:
    verb = _verb(tokens)
    if verb not in _STOP_VERBS:
        return False
    if verb == "wmic" and "delete" not in {token.lower() for token in tokens}:
        return False
    return any(_basename(token).lower() in _HERMES_IMAGES for token in tokens[1:])


def _stops_gateway(command: str) -> bool:
    from gateway.status import _gateway_command_subcommand
    return any(
        _gateway_command_subcommand(segment) in _GATEWAY_LIFECYCLE
        for segment in _segments(command)
    )


def _runs_restart_script(command: str) -> bool:
    for segment in _segments(command):
        tokens = _unwrap(_split_command(segment))
        if _verb(tokens) in _READ_VERBS:
            continue
        if any(_basename(token).lower() in _RESTART_SCRIPTS for token in tokens):
            return True
    return False


def _npm_mutates_install(tokens: list[str], cwd: str, install: Path) -> bool:
    if _verb(tokens) not in _NPM_VERBS:
        return False
    if not any(token.lower() in _NPM_MUTATIONS for token in tokens):
        return False
    prefix = _flag_value(tokens, "--prefix")
    if prefix and _same_or_under(_resolve(prefix, cwd), install):
        return True
    return _same_or_under(_resolve(cwd, cwd) if cwd else None, install)


def _moves_under(tokens: list[str], cwd: str, roots: tuple[Path, ...]) -> bool:
    if _verb(tokens) not in _MOVE_VERBS:
        return False
    return any(_under_any(_resolve(token, cwd), roots) for token in _operands(tokens))


def _writes_protected(segment: str, tokens: list[str], cwd: str, roots: tuple[Path, ...]) -> bool:
    if _redirects_into(segment, cwd, roots):
        return True
    verb = _verb(tokens)
    if verb in _READ_VERBS:
        return False
    if verb == "git":
        return _git_write_hits(tokens, cwd, roots)
    if verb in _WRITE_VERBS:
        return any(_under_any(_resolve(token, cwd), roots) for token in _operands(tokens))
    return False


def _redirects_into(segment: str, cwd: str, roots: tuple[Path, ...]) -> bool:
    return any(
        _under_any(_resolve(match.group(1), cwd), roots)
        for match in _REDIRECT.finditer(segment)
    )


def _git_write_hits(tokens: list[str], cwd: str, roots: tuple[Path, ...]) -> bool:
    operands = _operands(tokens)
    if not any(token.lower() in _GIT_WRITE for token in operands):
        return False
    if any(_under_any(_resolve(token, cwd), roots) for token in operands):
        return True
    return _same_or_under(_resolve(cwd, cwd) if cwd else None, roots[0])


def _operands(tokens: list[str]) -> list[str]:
    values: list[str] = []
    skip = False
    for token in tokens[1:]:
        if skip:
            values.append(token)
            skip = False
            continue
        if token.lower() in _VALUE_FLAGS:
            skip = True
            continue
        if token.startswith("-"):
            continue
        values.append(token)
    return values


def _flag_value(tokens: list[str], flag: str) -> str | None:
    for token in tokens:
        if token.lower().startswith(flag + "="):
            return token.split("=", 1)[1]
    for index, token in enumerate(tokens):
        if token.lower() == flag and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _resolve(token: str, cwd: str) -> Path | None:
    cleaned = os.path.expandvars(token.strip().strip("\""))
    if not cleaned or cleaned.startswith("-"):
        return None
    path = Path(cleaned)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        return path.resolve()
    except OSError:
        return None


def _under_any(path: Path | None, roots: tuple[Path, ...]) -> bool:
    return path is not None and any(_same_or_under(path, root) for root in roots)


def _same_or_under(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    try:
        left = os.path.normcase(str(path.resolve()))
        right = os.path.normcase(str(root.resolve()))
    except OSError:
        return False
    if left == right:
        return True
    prefix = right if right.endswith(os.sep) else right + os.sep
    return left.startswith(prefix)
