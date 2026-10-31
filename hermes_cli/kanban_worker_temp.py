"""Kanban worker TEMP routing and the worker-temp janitor.

When ``kanban.worker_temp_root`` is set, every dispatcher-spawned worker gets
TEMP/TMP/TMPDIR under ``<root>/<assignee>/<task_id>_<run_id>``. When unset, the
inherited env is left alone — never a quiet C: fallback. The janitor walks the
root on a gateway timer (and via ``hermes kanban gc``) and deletes run folders
older than ``kanban.worker_temp_max_age_hours``, except folders whose owner pid
is still alive.

When the configured root's leaf is ``_tmp``, the same pass also sweeps sibling
``<parent>/<agent>/tmp`` trees (legacy agent temp outside ``_tmp``). Deletes
clear the read-only bit on retry so Windows git object files do not strand
folders; every failed path is logged with its size.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional

logger = logging.getLogger("hermes.kanban.worker_temp")

# Leaf folder: task id + "_" + (run id digits | "no-run"). Task ids in this
# tree are ``t_`` + hex; keep the gate loose enough for stable ids, tight
# enough to refuse drive litter and random names.
_RUN_FOLDER_RE = re.compile(r"^(t_[0-9a-fA-F]+)_(?:no-run|\d+)$")
_OWNER_MARKER = ".owner.json"
_SETTING_ROOT = "kanban.worker_temp_root"
_SETTING_MAX_AGE = "kanban.worker_temp_max_age_hours"
_SETTING_JANITOR = "kanban.worker_temp_janitor_minutes"


@dataclass(frozen=True)
class WorkerTempSettings:
    root: Optional[str]
    max_age_hours: int
    janitor_minutes: int


@dataclass(frozen=True)
class SweepResult:
    removed: int
    bytes_freed: int
    refused: int
    skipped_live: int
    skipped_young: int


def _positive_int(raw: Any, default: int) -> int:
    try:
        if raw is None or raw is False or raw == "":
            return default
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def load_worker_temp_settings(kanban_cfg: Optional[Mapping[str, Any]] = None) -> WorkerTempSettings:
    """Read the three named settings from a ``kanban:`` block (or live config)."""
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception:
            kanban_cfg = {}
    if not isinstance(kanban_cfg, Mapping):
        kanban_cfg = {}
    raw_root = kanban_cfg.get("worker_temp_root")
    root: Optional[str]
    if raw_root is None or raw_root is False:
        root = None
    else:
        text = str(raw_root).strip()
        root = text or None
    return WorkerTempSettings(
        root=root,
        max_age_hours=_positive_int(kanban_cfg.get("worker_temp_max_age_hours"), 48),
        janitor_minutes=_positive_int(kanban_cfg.get("worker_temp_janitor_minutes"), 60),
    )


def run_folder_name(task_id: str, run_id: Any) -> str:
    """``<task id>_<run id>``; ``_no-run`` when the run id is absent."""
    tid = (task_id or "").strip() or "unknown"
    if run_id is None or run_id is False or run_id == "":
        return f"{tid}_no-run"
    return f"{tid}_{int(run_id)}"


def _is_reparse_point(path: Path) -> bool:
    """True for symlinks and Windows junctions/mount-points."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        if attrs == -1:
            return False
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except Exception:
        return False


def _is_unc_path(path: Path) -> bool:
    s = str(path)
    return s.startswith("\\\\") or s.startswith("//")


def _is_drive_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    # POSIX /
    if str(resolved) in ("/", "\\"):
        return True
    # Windows ``S:\`` / ``S:/``
    parts = resolved.parts
    if os.name == "nt" and len(parts) == 1 and parts[0].endswith((":", ":\\", ":/")):
        return True
    if os.name == "nt" and len(parts) == 1 and re.fullmatch(r"[A-Za-z]:\\?", parts[0]):
        return True
    # Path.anchor equals the full path for a drive root
    try:
        return resolved == resolved.anchor or str(resolved).rstrip("\\/") == str(resolved.anchor).rstrip("\\/")
    except Exception:
        return False


def validate_worker_temp_root(root: str | Path, *, log: Callable[..., None] | None = None) -> Optional[Path]:
    """Resolve *root* for use, or return None after logging why it is refused.

    Refuses UNC paths, drive roots, and paths whose device no longer resolves.
    """
    log = log or logger.warning
    try:
        raw = Path(root)
    except (TypeError, ValueError) as exc:
        log("%s refused: invalid path %r (%s)", _SETTING_ROOT, root, exc)
        return None
    if _is_unc_path(raw):
        log("%s refused: UNC path %s is not allowed", _SETTING_ROOT, raw)
        return None
    try:
        # resolve(strict=False) still fails when the drive letter is missing
        resolved = raw.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        log("%s refused: path %s does not resolve (%s)", _SETTING_ROOT, raw, exc)
        return None
    if _is_unc_path(resolved):
        log("%s refused: UNC path %s is not allowed", _SETTING_ROOT, resolved)
        return None
    if _is_drive_root(resolved):
        log("%s refused: drive root %s is not allowed", _SETTING_ROOT, resolved)
        return None
    # Device gone: on Windows a missing drive often still "resolves" to itself;
    # probe the anchor.
    anchor = Path(resolved.anchor) if resolved.anchor else resolved
    try:
        if not anchor.exists() and not resolved.exists():
            # Parent may be creatable on an existing volume; only fail when the
            # volume itself is missing.
            drive = resolved.drive
            if drive and not Path(drive + os.sep).exists():
                log("%s refused: device for %s no longer resolves", _SETTING_ROOT, resolved)
                return None
    except OSError as exc:
        log("%s refused: path %s does not resolve (%s)", _SETTING_ROOT, resolved, exc)
        return None
    return resolved


def _strictly_below(child: Path, root: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return child.resolve() != root.resolve()
    except (ValueError, OSError):
        return False


def prepare_worker_temp_dir(
    *,
    root: str | Path,
    assignee: str,
    task_id: str,
    run_id: Any,
    log: Callable[..., None] | None = None,
) -> Optional[Path]:
    """Create ``<root>/<assignee>/<task_id>_<run>`` or return None on soft failure."""
    log = log or logger.error
    resolved = validate_worker_temp_root(root, log=log)
    if resolved is None:
        return None
    agent = (assignee or "").strip() or "_unassigned"
    # Keep assignee a single path segment.
    agent = agent.replace("/", "_").replace("\\", "_").replace("..", "_")
    leaf = run_folder_name(task_id, run_id)
    if not _RUN_FOLDER_RE.match(leaf):
        # Still allow non-t_ ids if the dispatcher ever uses them: only refuse
        # empty / traversal junk.
        if not leaf or ".." in leaf or "/" in leaf or "\\" in leaf:
            log("%s: refusing unsafe run folder name %r under %s", _SETTING_ROOT, leaf, resolved)
            return None
    target = resolved / agent / leaf
    if not _strictly_below(target, resolved):
        log("%s: refusing path %s (not strictly below root %s)", _SETTING_ROOT, target, resolved)
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(
            "%s unusable at %s (%s); leaving worker temp env unchanged. "
            "Fix the path or free the volume, then restart the gateway.",
            _SETTING_ROOT,
            target,
            exc,
        )
        return None
    return target


def write_owner_marker(folder: Path, *, pid: int, task_id: str, started_at: Optional[float] = None) -> None:
    """Write ``.owner.json`` so the janitor can keep a live worker's folder."""
    payload = {
        "pid": int(pid),
        "task_id": task_id,
        "started_at": float(started_at if started_at is not None else time.time()),
    }
    path = folder / _OWNER_MARKER
    try:
        path.write_text(json.dumps(payload, indent=None, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("kanban worker temp: could not write %s (%s)", path, exc)


def apply_worker_temp_env(
    env: MutableMapping[str, str],
    *,
    assignee: str,
    task_id: str,
    run_id: Any = None,
    kanban_cfg: Optional[Mapping[str, Any]] = None,
    log_warning: Callable[..., None] | None = None,
    log_error: Callable[..., None] | None = None,
) -> Optional[Path]:
    """Pin TEMP/TMP/TMPDIR for one spawn. Returns the run folder, or None when skipped.

    Root unset → one warning naming the setting; env unchanged.
    Root set but unusable → one error naming setting + path; env unchanged; spawn proceeds.
    """
    warn = log_warning or logger.warning
    err = log_error or logger.error
    settings = load_worker_temp_settings(kanban_cfg)
    if not settings.root:
        warn(
            "%s is not set; worker inherits the gateway temp path. "
            "Set it (for example S:\\ForceAgents\\_tmp) to keep test temp off C:.",
            _SETTING_ROOT,
        )
        return None
    folder = prepare_worker_temp_dir(
        root=settings.root,
        assignee=assignee,
        task_id=task_id,
        run_id=run_id,
        log=err,
    )
    if folder is None:
        return None
    path_str = str(folder)
    env["TEMP"] = path_str
    env["TMP"] = path_str
    env["TMPDIR"] = path_str
    env["HERMES_WORKER_TEMP_ROOT"] = path_str
    return folder


def _pid_alive(pid: int) -> Optional[bool]:
    """True/False when known; None when the check itself failed."""
    if pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(pid))
    except Exception:
        try:
            from hermes_cli._subprocess_compat import pid_exists_stdlib

            return bool(pid_exists_stdlib(pid))
        except Exception:
            return None


def _read_owner(folder: Path) -> Optional[dict[str, Any]]:
    path = folder / _OWNER_MARKER
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _newest_mtime(folder: Path) -> float:
    newest = 0.0
    try:
        newest = folder.stat().st_mtime
    except OSError:
        pass
    try:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            # Do not descend into reparse points
            keep = []
            for name in dirnames:
                p = Path(dirpath) / name
                if _is_reparse_point(p):
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    newest = max(newest, p.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def _dir_size_bytes(folder: Path) -> int:
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            keep = []
            for name in dirnames:
                p = Path(dirpath) / name
                if _is_reparse_point(p):
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / (1024 ** 3):.1f} GB"


def _clear_readonly(path: str | os.PathLike[str] | Path) -> None:
    """Clear the read-only bit so a retry can delete Windows git objects."""
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IWRITE | stat.S_IWUSR)
    except OSError:
        pass


def _rmtree_with_readonly_retry(folder: Path) -> None:
    """``shutil.rmtree`` that clears read-only bits on retry (Windows git objects).

    Architect measure (t_c7760d5e): plain rmtree fails on any folder that holds a
    repo because git marks object files read-only. Clear the bit and retry once
    per path via onexc/onerror.
    """

    def _onexc(func: Callable[..., Any], path: str, exc: BaseException) -> None:
        _clear_readonly(path)
        try:
            func(path)
        except OSError:
            # Let the outer caller log path + size; re-raise the original class.
            raise exc

    try:
        shutil.rmtree(folder, onexc=_onexc)
    except TypeError:
        # Python < 3.12: onerror(func, path, exc_info)
        def _onerror(func: Callable[..., Any], path: str, exc_info: Any) -> None:
            _clear_readonly(path)
            try:
                func(path)
            except OSError:
                raise

        shutil.rmtree(folder, onerror=_onerror)


def _legacy_agent_tmp_roots(primary: Path) -> list[Path]:
    """Extra sweep roots for pre-_tmp agent temp piles.

    Decision on t_c7760d5e (Architect measure ~14 GB outside ``_tmp``): when the
    configured root's leaf is ``_tmp``, also sweep sibling
    ``<parent>/<agent>/tmp`` directories (hera/tmp, darkeyes/tmp, …). New spawns
    still land only under ``_tmp``; this multi-root pass is what reaches the
    legacy pile so a 48h rule is not a no-op for existing disk.
    """
    if primary.name.lower() != "_tmp":
        return []
    parent = primary.parent
    out: list[Path] = []
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name.lower() == "_tmp":
            continue
        if _is_reparse_point(entry) or not entry.is_dir():
            continue
        candidate = entry / "tmp"
        if _is_reparse_point(candidate):
            continue
        if candidate.is_dir():
            out.append(candidate)
    return out


def _should_keep_for_owner(owner: Optional[dict[str, Any]], *, max_age_seconds: float, now: float) -> tuple[bool, str]:
    """Return (keep, reason)."""
    if not owner:
        return False, "no-owner"
    raw_pid = owner.get("pid")
    pid = -1
    if raw_pid is not None and raw_pid is not False and raw_pid != "":
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = -1
    alive = _pid_alive(pid) if pid > 0 else False
    if alive is True:
        return True, "pid-alive"
    if alive is None:
        started = owner.get("started_at")
        started_f: Optional[float] = None
        if started is not None and started is not False and started != "":
            try:
                started_f = float(started)
            except (TypeError, ValueError):
                started_f = None
        if started_f is not None and (now - started_f) < max_age_seconds:
            return True, "pid-unchecked-young-owner"
        # Fall through: age by mtime decides.
    return False, "owner-dead"


def _sweep_one_tree(
    resolved: Path,
    *,
    max_age_seconds: float,
    clock: float,
    warn: Callable[..., None],
) -> SweepResult:
    """Walk one temp tree: either ``_tmp/<agent>/<run>`` or legacy ``<agent>/tmp/<run>``."""
    removed = 0
    bytes_freed = 0
    refused = 0
    skipped_live = 0
    skipped_young = 0

    try:
        top_entries = list(resolved.iterdir())
    except OSError as exc:
        warn("worker-temp janitor: cannot list %s (%s)", resolved, exc)
        return SweepResult(0, 0, 1, 0, 0)

    # Primary shape: root / assignee / run_folder
    # Legacy shape: agent_tmp_root already IS the "tmp" dir, so children are run folders.
    # Detect: if any child matches run-folder RE, treat resolved as the assignee-level tmp.
    run_like = 0
    dir_count = 0
    for entry in top_entries:
        if entry.is_dir() and not _is_reparse_point(entry):
            dir_count += 1
            if _RUN_FOLDER_RE.match(entry.name):
                run_like += 1
    legacy_flat = dir_count > 0 and run_like >= max(1, dir_count // 2)

    if legacy_flat:
        assignee_buckets = [resolved]
    else:
        assignee_buckets = [e for e in top_entries if e.is_dir()]

    for assignee_dir in assignee_buckets:
        if _is_reparse_point(assignee_dir):
            warn("worker-temp janitor skipped symlink/junction: %s", assignee_dir)
            refused += 1
            continue
        if not assignee_dir.is_dir():
            continue
        if not _strictly_below(assignee_dir, resolved) and assignee_dir.resolve() != resolved.resolve():
            warn("worker-temp janitor refused path not under root: %s", assignee_dir)
            refused += 1
            continue
        try:
            if assignee_dir.resolve() == resolved.resolve():
                run_entries = top_entries
            else:
                run_entries = list(assignee_dir.iterdir())
        except OSError as exc:
            warn("worker-temp janitor: cannot list %s (%s)", assignee_dir, exc)
            refused += 1
            continue
        for run_dir in run_entries:
            if _is_reparse_point(run_dir):
                warn("worker-temp janitor skipped symlink/junction: %s", run_dir)
                refused += 1
                continue
            if not run_dir.is_dir():
                continue
            # Safety: target must be strictly below the tree root (never the root).
            if not _strictly_below(run_dir, resolved):
                warn(
                    "worker-temp janitor refused: %s is not strictly below root %s",
                    run_dir,
                    resolved,
                )
                refused += 1
                continue
            if run_dir.resolve() == resolved.resolve():
                warn("worker-temp janitor refused: would delete the root itself (%s)", run_dir)
                refused += 1
                continue
            if not _RUN_FOLDER_RE.match(run_dir.name):
                warn(
                    "worker-temp janitor refused: folder name %r is not a task-id run folder",
                    run_dir.name,
                )
                refused += 1
                continue

            owner = _read_owner(run_dir)
            keep, reason = _should_keep_for_owner(owner, max_age_seconds=max_age_seconds, now=clock)
            if keep and reason == "pid-alive":
                skipped_live += 1
                continue
            if keep and reason == "pid-unchecked-young-owner":
                skipped_young += 1
                continue

            newest = _newest_mtime(run_dir)
            age = clock - newest if newest else clock
            if age < max_age_seconds:
                skipped_young += 1
                continue

            size = _dir_size_bytes(run_dir)
            try:
                _rmtree_with_readonly_retry(run_dir)
            except OSError as exc:
                warn(
                    "worker-temp janitor: failed to remove %s (%s B) — %s",
                    run_dir,
                    size,
                    exc,
                )
                refused += 1
                continue
            if run_dir.exists():
                warn(
                    "worker-temp janitor: failed to remove %s (%s B) — still present after rmtree",
                    run_dir,
                    size,
                )
                refused += 1
                continue
            removed += 1
            bytes_freed += size

        # Drop empty assignee dirs under primary tree only (not the legacy tmp root itself).
        if assignee_dir.resolve() != resolved.resolve():
            try:
                if assignee_dir.is_dir() and not any(assignee_dir.iterdir()):
                    assignee_dir.rmdir()
            except OSError:
                pass

    return SweepResult(removed, bytes_freed, refused, skipped_live, skipped_young)


def sweep_worker_temp(
    *,
    root: Optional[str | Path] = None,
    max_age_hours: Optional[int] = None,
    kanban_cfg: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    log: Callable[..., None] | None = None,
    log_info: Callable[..., None] | None = None,
) -> SweepResult:
    """One janitor pass. Safe no-op when root is unset or refused.

    Also sweeps legacy ``<parent>/<agent>/tmp`` when the configured root leaf is
    ``_tmp`` (multi-root decision on t_c7760d5e — reaches ~14 GB outside _tmp).
    """
    warn = log or logger.warning
    info = log_info or logger.info
    settings = load_worker_temp_settings(kanban_cfg)
    root_s = root if root is not None else settings.root
    age_h = max_age_hours if max_age_hours is not None else settings.max_age_hours
    if not root_s:
        info("%s unset; worker-temp janitor pass skipped", _SETTING_ROOT)
        return SweepResult(0, 0, 0, 0, 0)

    resolved = validate_worker_temp_root(root_s, log=warn)
    if resolved is None:
        return SweepResult(0, 0, 1, 0, 0)
    if _is_reparse_point(resolved):
        warn("worker-temp janitor refused: root %s is a symlink/junction", resolved)
        return SweepResult(0, 0, 1, 0, 0)

    max_age_seconds = max(1, int(age_h)) * 3600
    clock = time.time() if now is None else float(now)

    trees: list[Path] = []
    if resolved.exists() and resolved.is_dir():
        trees.append(resolved)
    elif not resolved.exists():
        info("worker-temp janitor: root %s does not exist yet; checking legacy agent tmp only", resolved)
    else:
        warn("worker-temp janitor refused: root %s is not a directory", resolved)
        return SweepResult(0, 0, 1, 0, 0)

    for legacy in _legacy_agent_tmp_roots(resolved):
        validated = validate_worker_temp_root(legacy, log=warn)
        if validated is None:
            continue
        if _is_reparse_point(validated) or not validated.is_dir():
            continue
        if validated not in trees:
            trees.append(validated)

    if not trees:
        info("worker-temp janitor: nothing to sweep under %s", resolved)
        return SweepResult(0, 0, 0, 0, 0)

    removed = 0
    bytes_freed = 0
    refused = 0
    skipped_live = 0
    skipped_young = 0
    for tree in trees:
        part = _sweep_one_tree(tree, max_age_seconds=max_age_seconds, clock=clock, warn=warn)
        removed += part.removed
        bytes_freed += part.bytes_freed
        refused += part.refused
        skipped_live += part.skipped_live
        skipped_young += part.skipped_young

    info(
        "worker-temp janitor: removed %d run folder(s), freed %s (refused=%d, live=%d, young=%d) under %s (+%d legacy root(s))",
        removed,
        _format_bytes(bytes_freed),
        refused,
        skipped_live,
        skipped_young,
        resolved,
        max(0, len(trees) - 1),
    )
    return SweepResult(removed, bytes_freed, refused, skipped_live, skipped_young)


def format_sweep_counts(result: SweepResult) -> str:
    """One printed line for ``hermes kanban gc`` and gateway logs."""
    return (
        f"worker-temp: removed {result.removed} run folder(s), "
        f"freed {_format_bytes(result.bytes_freed)} "
        f"(refused={result.refused}, live={result.skipped_live}, young={result.skipped_young})"
    )
