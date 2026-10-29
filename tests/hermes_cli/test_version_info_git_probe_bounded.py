"""``version_info._run_git`` is on the agent-build path (xAI's User-Agent asks for the version), so a
hung git must cost its timeout, never the turn. ``subprocess.run(timeout=…)`` is not that bound on
Windows: after the timeout it kills the direct child and then calls an unbounded ``communicate()``,
which waits for EOF on pipes a surviving descendant still holds. On 2026-10-29 every xAI Kanban
worker hung there for hours (``AIAgent.__init__`` → ``hermes_xai_default_headers`` →
``get_version_info`` → ``_run_git("rev-parse", "HEAD")`` with orphaned ``git.exe`` children)."""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from hermes_cli import version_info

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


def test_run_git_returns_within_its_bound_when_a_descendant_holds_the_pipes(fake_git, tmp_path):
    fake_git(f"""
        import subprocess, sys, time
        # Inherits this process's stdout/stderr (the caller's capture pipes) and outlives the kill.
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep({_HOLD_SECONDS})"])
        time.sleep({_HOLD_SECONDS})
    """)
    started = time.monotonic()
    result = version_info._run_git(tmp_path, "rev-parse", "HEAD")
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 10, f"_run_git blocked {elapsed:.1f}s past its 3s timeout"


def test_run_git_returns_stripped_stdout_of_a_successful_git(fake_git, tmp_path):
    fake_git("""
        print("  eda08b780ff048e8b9bbf052b4797cbe16a7018c  ")
    """)
    assert version_info._run_git(tmp_path, "rev-parse", "HEAD") == "eda08b780ff048e8b9bbf052b4797cbe16a7018c"


def test_run_git_returns_none_for_a_failing_or_silent_git(fake_git, tmp_path):
    fake_git("""
        import sys
        print("fatal: not a git repository")
        sys.exit(128)
    """)
    assert version_info._run_git(tmp_path, "rev-parse", "HEAD") is None
    fake_git("""
        pass
    """)
    assert version_info._run_git(tmp_path, "rev-parse", "HEAD") is None


def test_run_git_runs_in_the_repo_it_was_given(fake_git, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_git("""
        import os, sys
        args = sys.argv[1:]
        # Either form names the repo: ``git -C <repo> …`` or ``cwd=<repo>``.
        where = args[args.index("-C") + 1] if "-C" in args else os.getcwd()
        print(os.path.realpath(where))
    """)
    assert version_info._run_git(repo, "rev-parse", "HEAD") == os.path.realpath(repo)
