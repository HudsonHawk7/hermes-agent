"""Privacy-mode user pictures: stage for the turn, then delete.

A privacy file's name starts with ``privacy_`` so a crash sweep can find
leftovers without touching pictures the girls already made (those use
``img_``). The sweep looks only in the image cache folders the stager
writes, and only at regular files with a staged image suffix. It does not
walk the rest of the home, and it never deletes a ``.py`` file.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PRIVACY_PREFIX = "privacy_"
# Suffixes ``gateway.session_images._stage`` accepts for a privacy attach.
STAGED_PRIVACY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def is_privacy_staged_path(path: str | os.PathLike) -> bool:
    return Path(path).name.startswith(PRIVACY_PREFIX)


_pending_by_session: dict[str, list[str]] = {}


def note_privacy_staged(session: dict | None, path: str, session_id: str | None = None) -> None:
    if not is_privacy_staged_path(path):
        return
    if session is not None:
        session.setdefault("_privacy_images", []).append(str(path))
    if session_id:
        _pending_by_session.setdefault(session_id, []).append(str(path))


def release_privacy_paths(paths) -> list[str]:
    released: list[str] = []
    for raw in paths or []:
        path = Path(raw)
        if not is_privacy_staged_path(path):
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        released.append(str(path))
    return released


def release_session_privacy_images(session: dict | None, session_id: str | None = None) -> list[str]:
    paths: list[str] = []
    if isinstance(session, dict):
        paths.extend(session.pop("_privacy_images", []) or [])
        session_id = session_id or session.get("session_id") or session.get("id")
    if session_id:
        paths.extend(_pending_by_session.pop(str(session_id), []))
    released = release_privacy_paths(paths)
    if isinstance(session, dict) and isinstance(session.get("attached_images"), list):
        gone = set(released)
        session["attached_images"] = [item for item in session["attached_images"] if item not in gone]
    return released


def _cache_dir_inside(path: Path, root: Path) -> Path | None:
    """A real directory under *root*, or None. Symlinks are not followed."""
    if path.is_symlink() or not path.is_dir():
        return None
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path


def privacy_image_cache_dirs(root: str | os.PathLike) -> list[Path]:
    """Folders the stager writes: ``<home>/cache/images`` and each profile's."""
    root_path = Path(root)
    found: list[Path] = []
    own = _cache_dir_inside(root_path / "cache" / "images", root_path)
    if own is not None:
        found.append(own)
    profiles = root_path / "profiles"
    if _cache_dir_inside(profiles, root_path) is None:
        return found
    try:
        entries = list(profiles.iterdir())
    except OSError:
        logger.warning("privacy image sweep could not list %s", profiles, exc_info=True)
        return found
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        cache = _cache_dir_inside(entry / "cache" / "images", root_path)
        if cache is not None:
            found.append(cache)
    return found


def _is_staged_privacy_image(path: Path) -> bool:
    if not path.name.startswith(PRIVACY_PREFIX):
        return False
    suffix = path.suffix.lower()
    if suffix == ".py" or suffix not in STAGED_PRIVACY_SUFFIXES:
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def sweep_privacy_images(root: str | os.PathLike) -> list[str]:
    """Delete leftover privacy pictures in the home's image cache folders."""
    root_path = Path(root)
    if not root_path.exists():
        return []
    released: list[str] = []
    for folder in privacy_image_cache_dirs(root_path):
        try:
            names = list(folder.iterdir())
        except OSError:
            logger.warning("privacy image sweep could not list %s", folder, exc_info=True)
            continue
        for path in names:
            if not _is_staged_privacy_image(path):
                continue
            try:
                path.unlink()
            except OSError:
                logger.warning("privacy image sweep could not delete %s", path, exc_info=True)
                continue
            released.append(str(path))
    return released


def files_with_hash(root: str | os.PathLike, digest: str) -> list[str]:
    found: list[str] = []
    root_path = Path(root)
    if not root_path.exists():
        return found
    want = digest.lower()
    for dirpath, _dirs, files in os.walk(root_path):
        for name in files:
            path = Path(dirpath) / name
            try:
                got = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            if got == want:
                found.append(str(path))
    return found
