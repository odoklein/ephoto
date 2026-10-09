"""Bounded pool for the image pipelines.

Both pipelines are CPU-bound and hold decoded images in memory, so they must not run
on the event loop nor on Starlette's request threadpool, where forty uploads would mean
forty full-resolution images in RAM at once.  Requests submit work here and either
return straight away (intake) or wait for their own job (re-crop, manual creation);
either way at most `PROCESSING_WORKERS` images are being processed at any moment.

The pool lives in this process, which is why the service runs as a single uvicorn
worker.  The queue itself is durable: a dossier stays `processing` in the database until
its job finishes, and startup re-submits whatever a crash or a redeploy left behind.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("ephoto.jobs")


class Jobs:
    def __init__(self) -> None:
        self._pool: ThreadPoolExecutor | None = None
        self._workers = 2
        self._lock = threading.Lock()
        self._pending = 0

    def start(self, workers: int) -> None:
        with self._lock:
            self._workers = max(1, workers)
            if self._pool is None:
                # Cancelled jobs of a previous pool never ran their bookkeeping.
                self._pending = 0
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="ephoto-job")

    def _ensure(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="ephoto-job")
            return self._pool

    def submit(self, function: Callable[..., Any], *args: Any) -> Future:
        pool = self._ensure()
        with self._lock:
            self._pending += 1

        def tracked() -> Any:
            try:
                return function(*args)
            except Exception:
                log.exception("job %s failed", getattr(function, "__name__", function))
                raise
            finally:
                with self._lock:
                    self._pending -= 1

        return pool.submit(tracked)

    def run(self, function: Callable[..., Any], *args: Any) -> Any:
        """Run on the pool and wait: for sync request handlers whose caller needs the result."""
        return self.submit(function, *args).result()

    async def run_async(self, function: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.wrap_future(self.submit(function, *args))

    @property
    def pending(self) -> int:
        """Jobs queued or running — exposed for the health report and the tests."""
        with self._lock:
            return self._pending

    def shutdown(self) -> None:
        # Jobs not yet started are cancelled: their dossiers stay `processing` and are
        # re-submitted at the next startup.  Running ones are allowed to finish.
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


jobs = Jobs()
