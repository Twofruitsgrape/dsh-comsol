"""Allow `python -m comsol_mcp` to run the full CLI.

Delegates to :func:`comsol_mcp.cli.main` so that `python -m comsol_mcp`, the
`comsol-mcp` console script and direct `cli.main([...])` calls all behave the
same: with no subcommand the MCP server is served on stdio, and `version` /
`detect` / `install` work as documented. Previously this module called
``server.main()`` directly, so every subcommand was silently ignored and the
server started instead.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
