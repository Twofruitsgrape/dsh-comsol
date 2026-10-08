"""Installer: write this MCP server into an agent's configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import writer
from .agents import AGENTS, DEFAULT_SERVER_NAME, agent_ids, detect_agents

DEFAULT_REPO_URL = "https://github.com/your-name/comsol-mcp"


def run_install(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else Path.cwd()

    if args.agent == "detect":
        print(json.dumps(detect_agents(project_dir), indent=2, ensure_ascii=False))
        return 0

    spec = AGENTS.get(args.agent)
    if spec is None:
        print(json.dumps({"error": f"Unknown agent '{args.agent}'", "agents": agent_ids()},
                         indent=2, ensure_ascii=False))
        return 2

    repo_url = args.url or DEFAULT_REPO_URL
    kind = writer.choose_command_kind(args.command, repo_url if args.url else "", project_dir)
    entry = writer.build_entry(kind, repo_url, project_dir)

    if args.print_snippet or args.agent == "generic":
        print(writer.snippet(spec, DEFAULT_SERVER_NAME, entry))
        return 0

    try:
        report = writer.install(spec, args.scope, DEFAULT_SERVER_NAME, entry,
                                project_dir, dry_run=args.dry_run)
    except writer.InstallError as exc:
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        return 2
    report["command_kind"] = kind
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0
