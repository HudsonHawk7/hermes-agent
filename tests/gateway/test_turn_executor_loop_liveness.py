"""A stalled Thread.start must not freeze the gateway event loop.

11/05 18:31: ``_UnboundedThreadExecutor.submit`` called ``Thread.start`` on the
loop thread while Windows commit was tight. ``start()`` blocked, the loop missed
liveness probes, and ``shutdown_watchdog`` dumped stacks and exited 75.

The production probe timeout is ``DEFAULT_LOOP_WATCHDOG_TIMEOUT_S`` (10s). This
file uses a compressed stand-in so the pin stays fast: ``Thread.start`` blocks
longer than the probe wait, and the loop must still answer
``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading

from gateway.turn_executor import _UnboundedThreadExecutor

# Compressed liveness interval. Production is 10s; the contract is the same.
_PROBE_TIMEOUT_S = 0.3
_START_BLOCK_S = 1.0


def test_blocking_thread_start_does_not_stall_event_loop(monkeypatch):
    assert _START_BLOCK_S > _PROBE_TIMEOUT_S

    start_entered = threading.Event()
    release_start = threading.Event()
    real_start = threading.Thread.start

    def blocking_start(self):
        # Stall only executor workers (``{prefix}_{n}``). The loop thread is
        # already running; a spawner thread must keep moving.
        name = self.name or ""
        if name.startswith("stall-test_"):
            start_entered.set()
            assert release_start.wait(30), "Thread.start was never released"
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", blocking_start)

    ex = _UnboundedThreadExecutor(thread_name_prefix="stall-test")
    loop_ready = threading.Event()
    loop_holder: dict = {}

    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_holder["loop"] = loop
        loop_ready.set()
        loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, name="stall-test-loop", daemon=True)
    loop_thread.start()
    assert loop_ready.wait(5), "test event loop never started"
    loop = loop_holder["loop"]

    submitted = threading.Event()

    def do_submit():
        try:
            ex.submit(lambda: None)
        finally:
            submitted.set()

    try:
        loop.call_soon_threadsafe(do_submit)
        assert start_entered.wait(5), "Thread.start was never entered"

        probe = threading.Event()
        loop.call_soon_threadsafe(probe.set)
        assert probe.wait(_PROBE_TIMEOUT_S), (
            "event loop missed a liveness probe while Thread.start blocked "
            f"longer than {_PROBE_TIMEOUT_S}s (the 11/05 gateway freeze)"
        )
    finally:
        release_start.set()
        submitted.wait(5)
        ex.shutdown(wait=True)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(5)
