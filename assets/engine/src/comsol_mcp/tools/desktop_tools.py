"""Desktop window tools: bring the COMSOL window forward, close it gracefully."""

from __future__ import annotations

from ..runtime import Runtime, guarded


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_focus_desktop() -> dict:
        """Bring the COMSOL desktop window to the front (Windows).

        Use it when the user asks to look at the model or results in COMSOL,
        so the window is visible without them hunting for it.
        """
        return await runtime.run(runtime.session.focus_desktop)

    @mcp.tool()
    @guarded
    async def comsol_close_desktop(dismiss_save_dialog: bool = True) -> dict:
        """Close the COMSOL desktop window gracefully - it is never force-killed.

        If COMSOL asks "save changes?", the dialog is answered with "No": the MCP
        keeps automatic snapshots under `.comsol-mcp/history/`, so nothing is
        lost. Closing the window before the server is stopped also prevents the
        stale `<model>.mph.lock` file that would make the next `-open` silently
        fail (see docs/troubleshooting.md).

        Args:
            dismiss_save_dialog: answer the save-changes dialog with "No"
                instead of leaving the window open.

        Works when the COMSOL window and its dialog are actually visible: the
        save dialog is dismissed with a real mouse click, and Windows does not
        let a background process raise another application's window, so a
        dialog hidden behind other windows cannot be reached. In that case the
        tool reports `still_open` and leaves everything untouched - ask the
        user to close the window (or answer the dialog) themselves.
        """
        return await runtime.run(runtime.session.close_desktop, dismiss_save_dialog)
