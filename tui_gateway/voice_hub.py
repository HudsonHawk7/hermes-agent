"""The Voice Hub — the gateway's one owner of the microphone.

Every surface arms through the Hub: the TUI, the desktop, and — because one gateway serves
every profile — a second profile's session inside this same process. The Hub is not a second
detector. It is the lease and the state machine in front of ``tools.wake_word``, so "who holds
the mic" has exactly one answer per box; the detector's own machine-wide file lock answers the
same question across processes.

Two rules it enforces (design t_e850e163, ratified after the 10/25 listening incident):

* **One owner.** A second surface asking for the mic while it is armed is refused with the
  detector's typed :class:`tools.wake_word.WakeWordInUse`, and the first owner keeps the
  device open exactly once.
* **A restart is not a person.** A Hub is born ``idle``, and nothing here reads
  ``wake_word.enabled`` to arm itself. Arming is a call from a surface, never a value replayed
  from disk or config at boot.

:meth:`VoiceHub.state` is the whole published surface: exactly four fields (VH1 AC4). The owner
transport rides along internally so pause/resume can find the caller, but it is never published
— a client draws the light from four fields and nothing else.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import suppress
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The three published statuses. ``idle`` is also "paused": capture is closed either way, and the
# four-field contract has no room for a fifth value (see ``stop_reason`` for which one it was).
ARMED = "armed"
IDLE = "idle"
STOPPED = "stopped"


class VoiceHub:
    """One owner, one state machine, one mic.

    Thread-safe: RPC handlers arrive on the pool, detection callbacks on their own thread, and
    the transport teardown path can release the lease from a third. Every transition moves
    ``since``, so a client can tell a fresh arm from a stale one without a second timestamp.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._status = IDLE
        self._surface: Optional[str] = None
        self._transport: object = None
        self._guard: Optional[Any] = None
        self._owns_guard = False
        self._since = float(clock())
        self._stop_reason: Optional[str] = None

    # ── published surface ────────────────────────────────────────────────────────────────────

    def state(self) -> dict:
        """The four fields a listening light draws. Never add a fifth: clients pin this shape."""
        with self._lock:
            return {
                "status": self._status,
                "owner_surface": self._surface,
                "since": self._since,
                "stop_reason": self._stop_reason,
            }

    def snapshot(self) -> tuple[object, str]:
        """``(owner_transport, owner_surface)``; ``(None, "")`` when no surface holds a claim.

        The claim outlives a pause (resume needs to find its caller) and dies on release, so a
        paused hub still names its surface here while ``state()["status"]`` reads ``idle``.
        """
        with self._lock:
            return self._transport, self._surface or ""

    # ── transitions ──────────────────────────────────────────────────────────────────────────

    def claim(self, surface: str, transport: object) -> None:
        """Take the mic for ``surface``. Raises ``WakeWordInUse`` when another caller holds it."""
        from tools.wake_word import WakeWordInUse

        with self._lock:
            if self._transport is not None and self._transport is not transport:
                raise WakeWordInUse(
                    f"Microphone is owned by the {self._surface or 'other'} surface.")
            self._transport = transport
            self._surface = surface or self._surface or "auto"

    def armed(self, surface: str, transport: object, guard: Any = None) -> dict:
        """Capture is open for ``surface`` — the arm succeeded, whatever the old state was.

        ``guard`` is the listening guard (``tools.voice_guard``) built for this arm, or ``None``
        when config does not ask for one. It lives exactly as long as the arm: the Hub never
        builds one itself and never reads config to decide, so a restarted gateway has no guard
        until a surface asks for the mic (VH1 AC1/AC3).
        """
        with self._lock:
            self._transport = transport
            self._surface = surface or self._surface or "auto"
            # The Hub owns a guard it was handed at arm time, so release/reset must close it.
            self._guard, self._owns_guard = guard, guard is not None
            self._status, self._stop_reason = ARMED, None
            self._since = float(self._clock())
            return self.state()

    def attach_guard(self, guard: Any) -> None:
        """Publish an already-running listener's guard for the doctor read. Reporting only.

        The listener owns this guard's lifetime; the Hub must never close it out from under a live
        capture stream, so ``attach_guard`` clears the ownership flag a claim may have set. ``None``
        clears the read — after that :meth:`guard_state` reports nothing rather than the counters of
        a guard that is already gone, which is what a restarted gateway has to look like.
        """
        with self._lock:
            self._guard, self._owns_guard = guard, False

    def guard_state(self) -> Optional[dict]:
        """The armed guard's counters, or ``None`` when no guard is in force.

        Deliberately NOT part of :meth:`state`: clients pin the four-field contract (VH1 AC4),
        and this is a doctor/log read (``voice_hub.guard``).
        """
        with self._lock:
            guard = self._guard
        if guard is None:
            return None
        try:
            return guard.state()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            # A closed/partial backend must not take a status read down — but say so in the log.
            logger.warning("voice_hub.guard: guard state unavailable: %s", exc, exc_info=True)
            return None

    def pause(self, transport: object, reason: str = "paused") -> bool:
        """Close capture but keep the claim (mic handed to a voice turn, phrase detected)."""
        with self._lock:
            return self._transition(transport, IDLE, reason)

    def resume(self, transport: object) -> bool:
        """Re-open capture for the caller that still holds the claim."""
        with self._lock:
            return self._transition(transport, ARMED, None)

    def release(self, transport: object, reason: str = "user_stop") -> bool:
        """Give the mic up entirely: the claim is gone, so another surface may arm."""
        with self._lock:
            if transport is None or transport is not self._transport:
                return False
            self._transport, self._surface = None, None
            self._status, self._stop_reason = STOPPED, reason
            self._since = float(self._clock())
            self._drop_guard()
            return True

    def reset(self) -> None:
        """Forget every claim — the boot state of a fresh gateway (tests use it for a restart)."""
        with self._lock:
            self._transport, self._surface, self._stop_reason = None, None, None
            self._status, self._since = IDLE, float(self._clock())
            self._drop_guard()

    def _drop_guard(self) -> None:
        """Forget the guard; close it only when the Hub owns it (caller holds the lock)."""
        guard, owns = self._guard, self._owns_guard
        self._guard, self._owns_guard = None, False
        if guard is not None and owns:
            with suppress(OSError, RuntimeError, ValueError):
                guard.close()

    def _transition(self, transport: object, status: str, reason: Optional[str]) -> bool:
        """Move to ``status`` iff ``transport`` holds the claim (caller holds the lock)."""
        if transport is None or transport is not self._transport:
            return False
        self._status, self._stop_reason = status, reason
        self._since = float(self._clock())
        return True


_hub_lock = threading.Lock()
_hub: Optional[VoiceHub] = None


def hub() -> VoiceHub:
    """This process's Hub. One per gateway: the microphone is a machine resource, not a profile's.

    Deliberately no config read and no arming here — a gateway that boots with
    ``wake_word.enabled: true`` still starts ``idle`` until a surface asks (VH1 AC1/AC3).
    """
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = VoiceHub()
        return _hub
