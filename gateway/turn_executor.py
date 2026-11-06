"""Unbounded executor for gateway turn bodies (see ``GatewayRunner._get_executor`` in gateway/run.py)."""

from __future__ import annotations

import concurrent.futures
import queue
import threading


_SENTINEL = object()


class _Work:
    __slots__ = ("args", "fn", "fut", "kwargs")

    def __init__(self, fut, fn, args, kwargs):
        self.fut = fut
        self.fn = fn
        self.args = args
        self.kwargs = kwargs


class _UnboundedThreadExecutor(concurrent.futures.Executor):
    """Unbounded turn pool: reuse workers, grow off the event-loop thread.

    ``ThreadPoolExecutor(max_workers=None)`` is NOT unbounded (it is ``min(32, cpu_count + 4)``),
    which is the same silent queue at a larger number. Growing that pool still calls
    ``Thread.start`` on the submitter. On the gateway that submitter is the event loop
    (``asyncio.run_in_executor`` → ``Executor.submit``), and a stalled ``start()`` — Windows
    commit exhaustion while a 17–23 GB model load held RAM — freezes liveness probes until
    the watchdog exits 75.

    No cap: a new turn must not queue behind others. Idle workers are reused so a later
    turn does not need a new stack. New OS threads are started only on a dedicated spawner
    thread; ``submit`` itself never calls ``Thread.start``. Exposes ``_threads`` and
    ``_shutdown`` like ``ThreadPoolExecutor`` so ``_stop_pool`` / ``_shutdown_executor``
    join and count its workers. Not ``tools.daemon_pool.DaemonThreadPoolExecutor(sys.maxsize)``:
    that still grows on the caller.
    """

    def __init__(self, thread_name_prefix: str = ""):
        self._prefix = thread_name_prefix
        self._threads: set = set()
        self._shutdown = False
        self._cancel_queued = False
        self._lock = threading.Lock()
        self._n = 0
        self._idle = 0
        self._work: queue.SimpleQueue = queue.SimpleQueue()
        self._spawn: queue.SimpleQueue = queue.SimpleQueue()
        spawner_name = f"{thread_name_prefix}-spawner" if thread_name_prefix else "unbounded-spawner"
        self._spawner = threading.Thread(target=self._spawn_loop, name=spawner_name, daemon=True)
        # One Thread.start at construction. Callers that construct on the loop pay this once;
        # every later growth is on this spawner, never on submit().
        self._spawner.start()
        self._threads.add(self._spawner)

    def submit(self, fn, /, *args, **kwargs):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        work = _Work(fut, fn, args, kwargs)
        # Must not call Thread.start, and must not wait on a lock the spawner holds across
        # start(): either would stall the gateway event loop the way 11/05 did.
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if self._idle > 0:
                self._idle -= 1
                self._work.put(work)
            else:
                self._n += 1
                self._spawn.put((work, self._n))
        return fut

    def _run_work(self, work: _Work) -> None:
        if not work.fut.set_running_or_notify_cancel():
            return
        try:
            work.fut.set_result(work.fn(*work.args, **work.kwargs))
        except BaseException as exc:
            work.fut.set_exception(exc)

    def _worker(self, first: _Work) -> None:
        work = first
        try:
            while work is not _SENTINEL:
                self._run_work(work)
                with self._lock:
                    if self._shutdown and self._cancel_queued:
                        return
                    self._idle += 1
                work = self._work.get()
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _fail(self, work: _Work, exc: BaseException) -> None:
        if not work.fut.done():
            work.fut.set_exception(exc)

    def _spawn_loop(self) -> None:
        while True:
            item = self._spawn.get()
            if item is _SENTINEL:
                while True:
                    try:
                        extra = self._spawn.get_nowait()
                    except queue.Empty:
                        break
                    if extra is not _SENTINEL:
                        extra_work, _n = extra
                        self._fail(
                            extra_work,
                            RuntimeError("cannot schedule new futures after shutdown"),
                        )
                return
            work, n = item
            with self._lock:
                cancel = self._shutdown and self._cancel_queued
            if cancel:
                self._fail(work, RuntimeError("cannot schedule new futures after shutdown"))
                continue
            t = threading.Thread(
                target=self._worker,
                args=(work,),
                name=f"{self._prefix}_{n}",
                daemon=True,
            )
            try:
                # Off the event loop. At the OS thread limit this raises; the Future
                # carries it and nothing unstarted is left in _threads for shutdown to join.
                t.start()
            except BaseException as exc:
                self._fail(work, exc)
                continue
            with self._lock:
                self._threads.add(t)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                self._cancel_queued = True
            idle = self._idle
        if cancel_futures:
            while True:
                try:
                    item = self._work.get_nowait()
                except queue.Empty:
                    break
                if item is not _SENTINEL:
                    item.fut.cancel()
        self._spawn.put(_SENTINEL)
        # Wake idle workers (a few extra pills cover the increment-then-get race).
        for _ in range(idle + 32):
            self._work.put(_SENTINEL)
        if wait:
            self._spawner.join()
            with self._lock:
                threads = [t for t in self._threads if t is not threading.current_thread()]
            for t in threads:
                t.join()
