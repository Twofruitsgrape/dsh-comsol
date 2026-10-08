"""Solving tools: run studies with streamed progress."""

from __future__ import annotations

import logging
import time

from mcp.server.fastmcp import Context

from .. import model_access
from ..errors import ComsolMcpError
from ..runtime import Runtime, guarded

log = logging.getLogger(__name__)


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_solve_start(study: str | None = None) -> dict:
        """Start solving a study in the background and return immediately.

        Use this for long solves: poll `comsol_solve_status` (percentages come
        from COMSOL's progress log) while the COMSOL desktop shows the solution
        being built live. COMSOL has no API to cancel a solve - the Stop button
        in its progress window does that.

        Args:
            study: study tag such as "std1" (see comsol_describe_model); omit
                to solve every study in the model.
        """
        def prepare():
            session = runtime.session
            model = session.main_model()
            if study:
                tags = [str(tag) for tag in model.java.study().tags()]
                if study not in tags:
                    raise ComsolMcpError(
                        f"Study '{study}' does not exist.",
                        hint=f"Available studies: {tags or 'none'}. "
                             "Call comsol_describe_model for details.",
                    )
            session.autosnapshot("before-solve")
            if session.progress is not None:
                session.progress.reset()

        await runtime.run(prepare)
        job = runtime.solve_job_start(study)
        return {
            **job,
            "note": "Solving in the background. Poll comsol_solve_status; "
                    "the COMSOL desktop shows the model live.",
        }

    @mcp.tool()
    @guarded
    async def comsol_solve_status() -> dict:
        """Report the background solve: state, progress percent/stage, result.

        Safe to call any time and as often as needed - reading status never
        touches the (busy) server, only COMSOL's progress log.
        """
        return runtime.solve_status()

    @mcp.tool()
    @guarded
    async def comsol_solve(study: str | None = None, ctx: Context = None) -> dict:
        """Solve a study (or all studies) and stream COMSOL progress into the conversation.

        An automatic snapshot is taken first, so the pre-solve state stays
        recoverable. The COMSOL desktop shows the model and the results live
        while this runs.

        Args:
            study: study tag such as "std1" (see comsol_describe_model). Omit to
                solve every study defined in the model.
        """
        session = runtime.session
        session.require_connected()
        started = time.time()

        def work():
            model = session.main_model()
            if study:
                tags = [str(tag) for tag in model.java.study().tags()]
                if study not in tags:
                    raise ComsolMcpError(
                        f"Study '{study}' does not exist.",
                        hint=f"Available studies: {tags or 'none'}. "
                             "Call comsol_describe_model for details.",
                    )
            session.autosnapshot("before-solve")
            if session.progress is not None:
                session.progress.reset()
            if study:
                model.java.study(study).run()  # by COMSOL tag, see _solve_work
            else:
                model.solve()
            return {
                "study": study or "all studies",
                "seconds": round(time.time() - started, 1),
                "solutions": model_access.solutions(model.java, max_items=20),
                "next": "Look at results with comsol_evaluate, comsol_export_plot_image "
                        "or comsol_show_results.",
            }

        return await runtime.run_streaming(ctx, work, poll=1.5)
