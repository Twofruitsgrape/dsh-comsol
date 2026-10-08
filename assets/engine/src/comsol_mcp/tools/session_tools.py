"""Session lifecycle tools: detect, start, attach, status, end."""

from __future__ import annotations

from .. import env
from ..runtime import Runtime, guarded


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_detect() -> dict:
        """Detect the COMSOL environment: installations, running desktop clients, open ports.

        Call this first whenever something about the environment is unclear, or
        before starting a session in a new workspace.
        """
        return await runtime.run(runtime.session.detect)

    @mcp.tool()
    @guarded
    async def comsol_start_session(
        model_path: str | None = None,
        port: int = env.DEFAULT_PORT,
        gui: bool = True,
        version: str | None = None,
    ) -> dict:
        """Start (or reuse) a COMSOL server and open the model in the visible COMSOL desktop.

        The desktop window is the point: it stays open for the whole session and
        shows every change, mesh build, solve progress and result in real time.

        Args:
            model_path: Absolute path of the .mph model to open. The COMSOL
                desktop opens this file, so it is the model the user sees. Omit
                to start with a fresh empty model in the desktop.
            port: Server port; the default COMSOL port is 2036. If a server is
                already listening here it is reused.
            gui: Keep True unless the user explicitly asked for headless work.
            version: COMSOL version to use, e.g. "6.2". Default: newest found.
        """
        return await runtime.run(runtime.session.start, model_path, port, gui, version)

    @mcp.tool()
    @guarded
    async def comsol_attach_session(port: int = env.DEFAULT_PORT, gui: bool = True) -> dict:
        """Attach to a COMSOL server that is already running (e.g. started by the user).

        Use when the user says a COMSOL server or desktop session is already up.
        """
        return await runtime.run(runtime.session.attach, port, gui)

    @mcp.tool()
    @guarded
    async def comsol_session_status() -> dict:
        """Report the current session: connection, port, main model, desktop window,
        progress mode."""
        return await runtime.run(runtime.session.status)

    @mcp.tool()
    @guarded
    async def comsol_end_session(save: bool = False, stop_server: bool = False,
                                 close_gui: bool = False) -> dict:
        """End the session: disconnect, optionally snapshot, close the desktop, stop the server.

        Args:
            save: write a snapshot of the main model before ending.
            stop_server: stop the COMSOL server if this session started it.
            close_gui: close the COMSOL desktop window (gracefully - a "save
                changes?" dialog is answered with No, snapshots keep the work).
                Recommended together with stop_server: it prevents the stale
                `<model>.mph.lock` file that otherwise breaks the next open.
        """
        return await runtime.run(runtime.session.end, save, stop_server, close_gui)
