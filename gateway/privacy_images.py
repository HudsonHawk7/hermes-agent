"""Privacy-mode user pictures: stage for the turn, then delete.

A privacy file's name starts with ``privacy_`` so a crash sweep can find
leftovers without touching pictures the girls already made (those use
``img_``). Sweep only deletes the privacy prefix.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

PRIVACY_PREFIX = "privacy_"


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


def sweep_privacy_images(root: str | os.PathLike) -> list[str]:
    """Delete leftover privacy_ files under HERMES_HOME (crash recovery)."""
    root_path = Path(root)
    if not root_path.exists():
        return []
    released: list[str] = []
    skip = {"node_modules", ".git", "environments", "venv", "__pycache__"}
    for dirpath, dirs, files in os.walk(root_path):
        dirs[:] = [name for name in dirs if name not in skip]
        for name in files:
            if not name.startswith(PRIVACY_PREFIX):
                continue
            path = Path(dirpath) / name
            try:
                path.unlink()
            except OSError:
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
