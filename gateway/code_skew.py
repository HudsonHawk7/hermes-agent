"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway's ``sys.modules`` is frozen at boot.  If the checkout is updated
underneath it, a first-time lazy import can resolve a freshly-pulled module
against a stale cached dependency -> ImportError.  We snapshot the revision at
startup so risky callers (e.g. ``/model`` switching) can refuse with a clear
"restart the gateway" message.  A revision move is skew only when files the
running process imports changed (Python modules, plus packaged data the
backend loads).  A desktop-only or docs-only fast-forward is not skew.  If
git cannot list the diff, detection still reports skew so a real Python
change is never missed.  If the revision can't be read (non-git install,
IO error) the boot snapshot stays ``None`` and detection no-ops — never a
false positive.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None


def _fingerprint() -> str | None:
    """Current checkout fingerprint via the CLI's worktree-aware git-rev reader
    (``hermes_cli.main`` is always already imported in a gateway process)."""
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha[:10] if sha and sha != "unresolved" and len(sha) > 10 else (sha or fingerprint)


def current_code_sha() -> str | None:
    """Full SHA for the checkout currently on disk, or None when unresolved."""
    fingerprint = _fingerprint()
    if fingerprint is None:
        return None
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha if sha and sha != "unresolved" else None


# Packaged data the running backend loads. Keep this in step with
# [tool.setuptools.package-data] in pyproject.toml. A desktop-only or
# docs-only fast-forward is not in this set.
_JSON_DATA_PREFIXES = (
    "hermes_cli/observability/schemas/",
    "hermes_cli/data/",
    "hermes_cli/local_runtime/",
)
_PACKAGED_DATA_EXACT = frozenset({
    "pm/lock.json",
    "pm/artifact-mirror.json",
    "pm/pyproject.toml",
    "pm/uv.lock",
    "tools/bot_desktop/launcher.sh",
    "tools/bot_desktop/wallpaper.png",
})
_PLUGIN_MANIFESTS = frozenset({"plugin.yaml", "plugin.yml"})


def _sha_from_fingerprint(fingerprint: str | None) -> str | None:
    """The git object name at the end of a revision fingerprint, or None."""
    if not fingerprint:
        return None
    sha = fingerprint.rsplit(":", 1)[-1].strip()
    if sha == "unresolved" or len(sha) < 7 or len(sha) > 64:
        return None
    if any(c not in "0123456789abcdefABCDEF" for c in sha):
        return None
    return sha


def _is_runtime_path(path: str) -> bool:
    """True when ``path`` is a file the running process imports or loads."""
    norm = path.replace("\\", "/").lstrip("./")
    if not norm or norm.endswith("/"):
        return False
    lower = norm.lower()
    if lower.endswith(".py"):
        return True
    if lower in _PACKAGED_DATA_EXACT:
        return True
    if lower.startswith("gateway/assets/"):
        return True
    if lower.startswith("plugins/") and lower.rsplit("/", 1)[-1] in _PLUGIN_MANIFESTS:
        return True
    return any(
        lower.startswith(prefix) and lower.endswith(".json")
        for prefix in _JSON_DATA_PREFIXES
    )


def _runtime_files_changed(boot_sha: str, disk_sha: str) -> bool | None:
    """Whether runtime files differ between the two revisions.

    ``None`` means git could not answer. Callers treat that as skew.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            [
                "git", "-C", str(_PROJECT_ROOT),
                "-c", "core.quotepath=false",
                "diff", "-z", "--name-only",
                f"{boot_sha}..{disk_sha}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return any(_is_runtime_path(name) for name in result.stdout.split("\0") if name)


def detect_code_skew() -> tuple[str, str] | None:
    """``(boot_rev, disk_rev)`` if runtime files drifted since boot, else ``None``.

    A desktop-only or docs-only fast-forward is not skew. If git cannot list
    the diff, report skew so a real Python change is never missed.
    """
    current = _fingerprint() if _boot_fingerprint is not None else None
    if current is None or current == _boot_fingerprint:
        return None
    boot_sha = _sha_from_fingerprint(_boot_fingerprint)
    disk_sha = _sha_from_fingerprint(current)
    if boot_sha and disk_sha and boot_sha == disk_sha:
        return None
    changed = (
        _runtime_files_changed(boot_sha, disk_sha)
        if boot_sha and disk_sha
        else None
    )
    if changed is False:
        return None
    return _short(_boot_fingerprint), _short(current)
