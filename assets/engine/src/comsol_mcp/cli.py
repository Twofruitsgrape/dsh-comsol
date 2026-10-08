"""Command line interface: serve the MCP server, detect the environment, install configs."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comsol-mcp",
        description="MCP server for COMSOL Multiphysics: let an AI agent read, modify, "
                    "mesh, solve and post-process models while the COMSOL desktop stays visible.",
    )
    # NOTE: the dest must not clash with the install subcommand's --command
    # option, which would otherwise overwrite the parsed subcommand name.
    sub = parser.add_subparsers(dest="subcommand")

    sub.add_parser("serve", help="Run the MCP server on stdio (default when no command is given).")
    sub.add_parser("detect", help="Print detected COMSOL installations, ports and desktop clients.")
    sub.add_parser("version", help="Print the version and exit.")

    install = sub.add_parser("install", help="Write this server into an agent's MCP configuration.")
    install.add_argument("--agent", required=True,
                         help="Agent id (zcode, claude, cursor, vscode, codex, gemini, windsurf, "
                              "claude-desktop, generic) or 'detect' to list what is installed.")
    install.add_argument("--scope", choices=["project", "global"], default="project",
                         help="project = only this workspace; global = every workspace.")
    install.add_argument("--command", choices=["auto", "uvx", "pip", "python", "local"],
                         default="auto", help="How the agent should launch the server.")
    install.add_argument("--project-dir", default=None,
                         help="Workspace directory for project scope (default: current directory).")
    install.add_argument("--url", default=None,
                         help="Git URL used for the uvx command form, e.g. "
                              "https://github.com/<you>/comsol-mcp")
    install.add_argument("--dry-run", action="store_true", help="Report without writing anything.")
    install.add_argument("--print", dest="print_snippet", action="store_true",
                         help="Print the configuration snippet instead of writing it.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand in (None, "serve"):
        from .server import main as serve_main
        serve_main()
        return 0

    if args.subcommand == "version":
        print(__version__)
        return 0

    if args.subcommand == "detect":
        from .env import DEFAULT_PORT, find_installations, port_open
        from .session import Session
        report = {
            "comsol_mcp": __version__,
            "python": sys.version.split()[0],
            "installs": [install.as_dict() for install in find_installations()],
            "gui_client_processes": Session._gui_client_processes(),
            "server_listening": {str(DEFAULT_PORT): port_open(DEFAULT_PORT)},
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "install":
        from .installer import run_install
        return run_install(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
