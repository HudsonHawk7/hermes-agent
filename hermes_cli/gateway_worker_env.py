"""Worker/delegation environment markers a gateway must never inherit.

The gateway OWNS its boards: its dispatcher mutates them on every tick. A gateway
launched from inside an agent or Kanban session -- Hermes Desktop started from a
worker's shell, ``hermes gateway run`` typed in one -- inherits that session's
``HERMES_DELEGATED_CHILD_CONTEXT`` and ``HERMES_KANBAN_*`` environment, and
``agent.delegation_context.kanban_path_is_fenced()`` then denies every board
mutation with ``PermissionError: delegate_task child contexts cannot mutate
Kanban tasks or boards``. Every dispatcher tick on every board dies, and the
board stays dead until the gateway is relaunched from a clean shell.

So a gateway strips these from ITS OWN environment before anything reads them.
The list matches the one our fleet's blocked-card sweep script pops for the same
reason: a fresh process must not inherit another process's worker fence or board
pins. ``hermes_cli.gateway_runtime_start.DAEMON_STRIPPED_ENV`` strips the same
names from a spawned daemon's environment; this is the in-process counterpart,
for a gateway started directly from a worker-shaped shell.
"""
from __future__ import annotations

import logging
import os
from typing import MutableMapping

#: Worker identity, delegation fence and board pins a gateway must not inherit.
WORKER_ENV_MARKERS: tuple[str, ...] = (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    # Root pins, not just the DB file. The dispatcher writes ``HERMES_KANBAN_WORKSPACES_ROOT``
    # into EVERY worker it spawns (``kanban_db_dispatch``), so a shell under a worker carries it;
    # the gateway's machine flows (notifier, watcher, dispatcher ticks) resolve every enumerated
    # board slug through these pins (``kanban_db._board_path``), and ``gateway.platforms.base``
    # reads the attachments pin when it serves a file. Inherited, every board's workspaces and
    # attachments land in another board's root while the board slug is ignored.
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)

_log = logging.getLogger(__name__)


def strip_worker_markers(
    env: MutableMapping[str, str] | None = None,
    *,
    logger: logging.Logger | None = None,
) -> tuple[str, ...]:
    """Pop every inherited worker/delegation marker from *env* (default ``os.environ``).

    Returns the names actually stripped (in :data:`WORKER_ENV_MARKERS` order) and logs ONE
    warning naming them, so the inheritance trap is visible in the gateway log instead of
    silently fencing the dispatcher off the boards it owns.
    """
    target = os.environ if env is None else env
    stripped = tuple(key for key in WORKER_ENV_MARKERS if key in target)
    if not stripped:
        return stripped
    for key in stripped:
        target.pop(key, None)
    (logger or _log).warning(
        "gateway inherited worker/delegation environment markers and stripped them before "
        "boot, so it owns its boards instead of being fenced off them: %s",
        ", ".join(stripped),
    )
    return stripped
