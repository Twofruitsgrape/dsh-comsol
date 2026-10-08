"""Execution runtime: one worker thread for COMSOL, one session, error rendering."""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import model_access
from .errors import ComsolMcpError
from .session import Session

log = logging.getLogger(__name__)

BUSY_MARKERS = ("Server is in use by another client",)
BUSY_TIMEOUT = float(os.environ.get("COMSOL_MCP_BUSY_TIMEOUT", "120"))
BUSY_RETRY_DELAY = 0.5
BUSY_RETRY_MAX_DELAY = 8.0

_JOB_COUNTER = itertools.count(1)


def run_with_busy_retry(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run `fn`, waiting out COMSOL's "Server is in use by another client".

    COMSOL refuses an operation while another client is busy - typically the
    desktop window while it is still loading a model, or while a modal dialog
    is open. The request never reached the server in that case, so retrying is
    safe. Verified on COMSOL 6.2: right after `comsol_start_session`, writes
    failed this way until the desktop had finished loading.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT
    delay = BUSY_RETRY_DELAY
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not any(marker in str(exc) for marker in BUSY_MARKERS):
                raise
            if time.monotonic() >= deadline:
                raise ComsolMcpError(
                    f"The COMSOL server stayed busy for {BUSY_TIMEOUT:.0f} s "
                    "(another client is working).",
                    hint="If the COMSOL desktop window shows a dialog (for example "
                         "'save changes?'), dismiss it and retry. A large model may also "
                         "simply still be loading.",
                ) from exc
            log.info("COMSOL server busy, retrying in %.1f s", delay)
            time.sleep(delay)
            delay = min(delay * 2, BUSY_RETRY_MAX_DELAY)


class Runtime:
    """Shared execution context of one MCP server process."""

    def __init__(self, home: Path | None = None):
        self.session = Session(home)
        self._solve_job: dict | None = None
        # Keep every COMSOL/JPype call on a single worker thread. JPype binds
        # threads to the JVM lazily, and MPh's client object is not designed
        # for concurrent use.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comsol")

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        return self.executor.submit(run_with_busy_retry, fn, *args, **kwargs)

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        name = getattr(fn, "__name__", repr(fn))
        log.debug("dispatching %s to the COMSOL worker thread", name)
        result = await loop.run_in_executor(
            self.executor, functools.partial(run_with_busy_retry, fn, *args, **kwargs)
        )
        log.debug("worker finished %s", name)
        return result

    # ---------------------------------------------------------------- solving

    def solve_job_start(self, study: str | None) -> dict:
        """Submit a study solve to the worker thread and return immediately.

        The worker thread is busy while the solve runs, but status queries only
        read COMSOL's progress file, so they stay responsive.
        """
        if self._solve_job is not None and not self._solve_job["future"].done():
            raise ComsolMcpError(
                "A solve is already running in the background.",
                hint="Call comsol_solve_status to follow it.",
            )
        job_id = f"solve-{next(_JOB_COUNTER)}"
        future = self.executor.submit(run_with_busy_retry, self._solve_work, study)
        self._solve_job = {
            "id": job_id,
            "study": study or "all studies",
            "future": future,
            "started": time.time(),
        }
        return {"job": job_id, "study": study or "all studies"}

    def _solve_work(self, study: str | None) -> dict:
        started = time.time()
        session = self.session
        model = session.main_model()
        if study:
            # MPh's model.solve(name) matches MPh node names, not COMSOL tags;
            # run the study by tag through the Java API instead.
            model.java.study(study).run()
        else:
            model.solve()
        return {
            "study": study or "all studies",
            "seconds": round(time.time() - started, 1),
            "solutions": model_access.solutions(model.java, max_items=20),
        }

    def solve_status(self) -> dict:
        """State of the background solve plus the newest COMSOL progress."""
        if self._solve_job is None:
            return {
                "state": "idle",
                "note": "No background solve was started (use comsol_solve_start).",
            }
        job = self._solve_job
        future = job["future"]
        state: dict[str, Any] = {
            "job": job["id"],
            "study": job["study"],
            "elapsed_seconds": round(time.time() - job["started"], 1),
        }
        progress = None
        stream = self.session.progress
        if stream is not None:
            lines = stream.read_new()
            progress = stream.parse(lines)
            if progress.get("last_line"):
                state["progress"] = progress
        if future.done():
            error = future.exception()
            if error is not None:
                state["state"] = "failed"
                state["error"] = str(error)[:1500]
                hint = getattr(error, "hint", None)
                if hint:
                    state["hint"] = hint
            else:
                state["state"] = "done"
                state["result"] = future.result()
        else:
            state["state"] = "solving"
        return state


    async def run_streaming(
        self,
        ctx: Any,
        fn: Callable[..., Any],
        *args: Any,
        poll: float = 2.0,
        **kwargs: Any,
    ) -> Any:
        """Run blocking `fn` on the worker thread while streaming COMSOL progress."""
        future = self.submit(fn, *args, **kwargs)
        while not future.done():
            await asyncio.sleep(poll)
            state, lines = await self.run(self.session.poll_progress)
            if not state:
                continue
            message = ""
            if self.session.progress is not None:
                message = self.session.progress.describe(state)
            try:
                await ctx.report_progress(float(state.get("percent") or 0), 100.0, message)
            except Exception:  # pragma: no cover - client may not support it
                pass
            try:
                for line in lines[-3:]:
                    await ctx.info(line)
            except Exception:  # pragma: no cover
                pass
        return future.result()


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn ComsolMcpError into a readable MCP tool error (hint included)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ComsolMcpError as exc:
            raise RuntimeError(exc.as_text()) from None

    return wrapper
