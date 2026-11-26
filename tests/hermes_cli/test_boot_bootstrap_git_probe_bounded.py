"""``boot_bootstrap.read_git_head`` runs on EVERY dispatch (``maybe_run_boot_bootstrap`` in
``hermes_cli/main.py``), before any work starts, so a hung git must cost its timeout and never
the boot. ``subprocess.run(timeout=…)`` is not that bound on Windows: after the timeout it kills
the direct child and then calls an unbounded ``communicate()``, which waits for EOF on pipes a
surviving descendant still holds. On 2026-11-26 about 15 Kanban worker starts froze behind
orphaned ``git.exe rev-parse HEAD`` children; the same class was fixed for
``version_info._run_git`` (``test_version_info_git_probe_bounded.py``) and this is its sibling.
"""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from hermes_cli import boot_bootstrap

# The fake git leaves a grandchild holding its stdout/stderr for this long, then hangs itself.
_HOLD_SECONDS = 20


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    """Route every ``git`` argv spawned through ``subprocess.Popen`` to a Python script, whatever
    helper spawns it, and return a writer for that script's body."""
    script = tmp_path / "fake_git.py"
    real_popen = subprocess.Popen

    class FakeGitPopen(real_popen):
        def __init__(self, args, *a, **kw):
            if isinstance(args, (list, tuple)) and args and os.path.basename(str(args[0])).lower() in ("git", "git.exe"):
                args = [sys.executable, str(script), *args[1:]]
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", FakeGitPopen)

    def write(body):
        script.write_text(textwrap.dedent(body), encoding="utf-8")
    return write


def test_read_git_head_returns_within_its_bound_when_a_descendant_holds_the_pipes(fake_git, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_git(f"""
        import subprocess, sys, time
        # Inherits this process's stdout/stderr (the caller's capture pipes) and outlives the kill.
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep({_HOLD_SECONDS})"])
        time.sleep({_HOLD_SECONDS})
    """)
    started = time.monotonic()
    result = boot_bootstrap.read_git_head(repo)
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 12, f"read_git_head blocked {elapsed:.1f}s past its 10s timeout"


def test_a_hung_git_skips_bootstrap_instead_of_blocking_boot(fake_git, tmp_path):
    """The None identity is the contract that keeps maintenance off a broken tree."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    fake_git(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep({_HOLD_SECONDS})"])
        time.sleep({_HOLD_SECONDS})
    """)
    started = time.monotonic()
    assert boot_bootstrap.current_install_identity(repo) is None
    assert boot_bootstrap.needs_bootstrap(repo) is None
    elapsed = time.monotonic() - started
    assert elapsed < 24, f"identity probes blocked {elapsed:.1f}s past their bounds"


def test_read_git_head_returns_stripped_stdout_of_a_successful_git(fake_git, tmp_path):
    fake_git("""
        print("  eda08b780ff048e8b9bbf052b4797cbe16a7018c  ")
    """)
    assert boot_bootstrap.read_git_head(tmp_path) == "eda08b780ff048e8b9bbf052b4797cbe16a7018c"


def test_read_git_head_returns_none_for_a_failing_or_silent_git(fake_git, tmp_path):
    fake_git("""
        import sys
        print("fatal: not a git repository")
        sys.exit(128)
    """)
    assert boot_bootstrap.read_git_head(tmp_path) is None
    fake_git("""
        pass
    """)
    assert boot_bootstrap.read_git_head(tmp_path) is None


def test_read_git_head_runs_in_the_repo_it_was_given(fake_git, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_git("""
        import os, sys
        args = sys.argv[1:]
        # Either form names the repo: ``git -C <repo> …`` or ``cwd=<repo>``.
        where = args[args.index("-C") + 1] if "-C" in args else os.getcwd()
        print(os.path.realpath(where))
    """)
    assert boot_bootstrap.read_git_head(repo) == os.path.realpath(repo)
