"""Model file tools: list, load, save, snapshots, progress mode."""

from __future__ import annotations

from ..runtime import Runtime, guarded


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_list_models() -> list[dict]:
        """List all models on the COMSOL server, with owner (desktop/mcp) and main-model flag."""
        return await runtime.run(runtime.session.list_models)

    @mcp.tool()
    @guarded
    async def comsol_create_model(name: str | None = None) -> dict:
        """Create a new empty model on the COMSOL server and make it the main model.

        Note: the COMSOL desktop only displays models it opened itself, so a
        model created this way is *not* shown in the COMSOL window. To see a
        fresh model in the desktop, start a session without `model_path` - the
        desktop then opens its own empty model, which becomes the main model.
        """
        def work():
            session = runtime.session
            main = session.create_model(name)
            return {"created": main.as_dict(), "models": session.list_models()}

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_load_model(path: str) -> dict:
        """Open a .mph file and make it the session's main model.

        If the model is already loaded on the server it is attached instead of
        re-loaded. Note: the COMSOL desktop only displays models it opened
        itself - to *see* this model in the desktop, start the session with
        `comsol_start_session(model_path=...)` instead.
        """
        return await runtime.run(runtime.session.open_model, path)

    @mcp.tool()
    @guarded
    async def comsol_save_model(path: str | None = None, allow_overwrite: bool = False) -> dict:
        """Save the main model to disk.

        Without `path`, saves into `<workspace>/.comsol-mcp/saved/` and never
        touches the original file. Saving over an existing file requires
        `allow_overwrite=True` (ask the user first).
        """
        target = await runtime.run(runtime.session.save, path, allow_overwrite)
        return {"saved_to": str(target)}

    @mcp.tool()
    @guarded
    async def comsol_snapshot(label: str | None = None) -> dict:
        """Save a snapshot copy of the main model into `.comsol-mcp/history/`."""
        target = await runtime.run(runtime.session.snapshot, label)
        return {"snapshot": str(target)}

    @mcp.tool()
    @guarded
    async def comsol_snapshot_list(limit: int = 20) -> list[dict]:
        """List recent snapshots (newest last) with timestamps and file paths."""
        return await runtime.run(runtime.session.snapshots.list, limit)

    @mcp.tool()
    @guarded
    async def comsol_set_progress_mode(mode: str = "stream") -> dict:
        """Choose how COMSOL reports progress.

        Args:
            mode: "stream" (default) logs structured progress that this MCP
                relays into the agent conversation and closes COMSOL's own
                progress window if it is open; "desktop" shows COMSOL's own
                progress window in the desktop instead; "off" disables both.
                The two are mutually exclusive in COMSOL.
        """
        active = await runtime.run(runtime.session.set_progress_mode, mode)
        return {"progress_mode": active}
