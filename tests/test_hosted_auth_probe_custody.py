"""Pin: hosted-room daemons start ``gateway.run`` inside a kill-on-close Job Object.

The 2-actor fixture is the leak class that was not on t_d43a65ca's list:
``test_two_authenticated_actors_own_separate_hosted_rooms`` starts two
``-m gateway.run`` children via ``hosted_auth_probe.daemon``. Those children
must keep riding ``process_custody.spawn``. This file does not spawn processes;
removing the Job Object assignment (``spawn(`` in ``daemon``) or replacing the
fixture's ``daemon()`` calls with ``subprocess.Popen`` is the mutation that
must go red here.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "gateway" / "fixtures" / "hosted_auth_probe.py"
RECOVERY = ROOT / "tests" / "gateway" / "fixtures" / "local_recovery_probe.py"
CUSTODY = ROOT / "tests" / "process_custody.py"


def _calls(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            count += 1
    return count


def _imported_names(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == module:
            found.update(alias.name for alias in node.names)
    return found


def test_hosted_auth_probe_gateway_run_children_use_kill_on_close_job():
    """Both hosted-room ``gateway.run`` children must use contained spawn."""
    recovery_imports = _imported_names(RECOVERY, "tests.process_custody")
    assert "spawn" in recovery_imports, (
        "local_recovery_probe no longer imports process_custody.spawn; "
        "hosted-room gateway.run children will orphan when the runner dies"
    )
    assert "kill_tree" in recovery_imports
    assert _calls(RECOVERY, "spawn") >= 1, (
        "daemon() no longer contained-spawns gateway.run; Job Object gone"
    )
    assert _calls(PROBE, "daemon") >= 2, (
        "hosted_auth_probe must start the host and profile `two` via daemon()"
    )
    assert _calls(PROBE, "Popen") == 0, (
        "hosted_auth_probe Popen'd a child; those pytest-temp trees orphan"
    )
    assert "daemon" in _imported_names(
        PROBE, "tests.gateway.fixtures.local_recovery_probe"
    )


def test_sweep_compares_homes_without_statting_them():
    """A live ``HERMES_HOME`` must not be ``Path.resolve()``'d (that stats it)."""
    src = CUSTODY.read_text(encoding="utf-8")
    assert "_norm(" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve"
        ):
            raise AssertionError("process_custody stats a home via Path.resolve()")

