"""An update cleanup must not delete a file the current commit still tracks.

A fork switch lists carry-only paths (``gateway/privacy_images.py``) as added
over the detached upstream checkout, then unlinks that list. After HEAD is the
carry commit those paths are tracked. Unlinking them is how the 21:03 update
removed the private-pictures module from a tree that still had it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def _init(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "force/carry")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")


def _carry_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init(root)
    tracked = root / "gateway" / "privacy_images.py"
    tracked.parent.mkdir()
    tracked.write_text("PRIVATE = 1\n", encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "carry")
    return root


def test_drop_added_files_leaves_a_file_head_still_tracks(tmp_path):
    root = _carry_repo(tmp_path)
    from hermes_cli.update_cmd_commit import drop_added_files

    assert drop_added_files(root, ["gateway/privacy_images.py"]) is True
    assert (root / "gateway" / "privacy_images.py").is_file()
    assert "gateway/privacy_images.py" in _git(root, "ls-files")


def test_drop_added_files_still_removes_a_file_head_does_not_track(tmp_path):
    root = tmp_path / "repo"
    _init(root)
    (root / "kept.py").write_text("k = 1\n", encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    extra = root / "gateway" / "privacy_images.py"
    extra.parent.mkdir()
    extra.write_text("not in HEAD\n", encoding="utf-8", newline="")
    from hermes_cli.update_cmd_commit import drop_added_files

    assert drop_added_files(root, ["gateway/privacy_images.py"]) is True
    assert not extra.exists()
    assert (root / "kept.py").is_file()


def test_put_back_paths_does_not_unlink_a_file_head_tracks(tmp_path):
    root = _carry_repo(tmp_path)

    def git(*args, stdin=None):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=stdin, capture_output=True, text=True, encoding="utf-8", check=False,
        )

    from hermes_cli._early_recovery import _put_back_paths

    assert _put_back_paths(git, root, [], ["gateway/privacy_images.py"], set()) is None
    assert (root / "gateway" / "privacy_images.py").is_file()
    assert "gateway/privacy_images.py" in _git(root, "ls-files")


def test_restore_missing_tracked_puts_a_deleted_carry_file_back(tmp_path):
    root = _carry_repo(tmp_path)
    tracked = root / "gateway" / "privacy_images.py"
    tracked.unlink()
    assert not tracked.exists()
    from hermes_cli.update_cmd_commit import restore_missing_tracked

    assert restore_missing_tracked(root) == 1
    assert tracked.is_file()
    assert tracked.read_text(encoding="utf-8") == "PRIVATE = 1\n"
