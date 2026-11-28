"""The dispatcher tick calls the version-controlled wake pass.

The gateway loop and the standalone daemon both call ``wake_on_dispatch_tick``.
A failure here is logged and swallowed so one bad pass cannot stop dispatch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from gateway.kanban_watchers_common import logger

_MOD = None


def _pass():
    """Load scripts/kanban_wake_pass.py once. Tests patch the returned module."""
    global _MOD
    if _MOD is None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "kanban_wake_pass.py"
        spec = importlib.util.spec_from_file_location("kanban_wake_pass_timer", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD = mod
    return _MOD


def wake_on_dispatch_tick(*, apply: bool) -> str:
    """Run one wake pass and log every line the tick log should quote.

    Returns the summary line. Never raises.
    """
    try:
        lines = _pass().run_for_timer(apply=apply)
    except Exception:
        logger.exception("kanban dispatcher: wake pass failed")
        return "wake-pass: failed"
    summary = lines[-1] if lines else "wake-pass: boards=0 lines=0 apply=0"
    for line in lines:
        logger.info("kanban dispatcher: %s", line)
    return summary
