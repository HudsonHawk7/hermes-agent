"""The isolated environment a checkout's test suite runs under.

Activation builds it beside the checkout's install state, and CI builds it the
same way. It carries the locked project dependencies plus the ``dev`` and
``test`` groups. It is a side environment
(``ensure_project_environment``), never the selected application generation:
test-only dependencies must not enter PM facts or anything that ships.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pm.environments import install_state_dir

NAME = "test-environment"
# The default developer environment includes the app's normal features.
# CI lanes pass their wider provider matrix explicitly.
DEFAULT_TEST_EXTRAS = ("all",)
GROUPS = ("dev", "test")


def testenv_root(project_root: Path) -> Path:
    import os

    # Optional off-C root (set after checking free space). Default stays under installs/.
    override = (os.environ.get("HERMES_TESTENV_ROOT") or "").strip()
    if override:
        from pm.environments import install_key

        return Path(override) / install_key(project_root) / NAME
    return install_state_dir(project_root) / NAME


def ensure_testenv(project_root: Path, extras: Sequence[str] | None = None) -> Path:
    """Build the test environment unless its locked inputs are unchanged; return its python."""
    from pm import ensure_project_environment

    chosen = sorted(set(DEFAULT_TEST_EXTRAS if extras is None else extras))
    return ensure_project_environment(NAME, project_root, extras=chosen, groups=GROUPS,
                                      root=testenv_root(project_root), explicit=True)


def testenv_python(project_root: Path) -> Path | None:
    """The selected test interpreter, read without acquiring tools or writing state."""
    from pm.operations import environment_python

    return environment_python(NAME, root=testenv_root(project_root))


def parse_extras(value: str) -> list[str] | None:
    """An empty value selects default extras; commas and spaces separate overrides."""
    return value.replace(",", " ").split() or None


def drop_testenv(project_root: Path) -> bool:
    """Remove the test-environment tree for *project_root* after a green suite.

    Keeps C: free of durable test venvs (GM no-dev-files-on-C). Returns True when
    nothing remains under the testenv root.
    """
    import shutil

    root = testenv_root(project_root)
    if not root.exists():
        return True
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()
