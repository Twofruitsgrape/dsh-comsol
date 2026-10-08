"""MCP tool registration."""

from __future__ import annotations

from ..runtime import Runtime


def register_all(mcp, runtime: Runtime) -> None:
    from . import (
        desktop_tools,
        inspect_tools,
        model_tools,
        modify_tools,
        post_tools,
        session_tools,
        solve_tools,
    )

    session_tools.register(mcp, runtime)
    model_tools.register(mcp, runtime)
    desktop_tools.register(mcp, runtime)
    inspect_tools.register(mcp, runtime)
    modify_tools.register(mcp, runtime)
    solve_tools.register(mcp, runtime)
    post_tools.register(mcp, runtime)
