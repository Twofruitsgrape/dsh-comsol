"""Configuration matrices for the agents we support out of the box."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERVER_NAME = "comsol"


def _appdata(*parts: str) -> str:
    base = os.environ.get("APPDATA") or "~/AppData/Roaming"
    return str(Path(base).joinpath(*parts))


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    global_path: str | None = None
    project_path: str | None = None
    key_path: tuple[str, ...] = ("mcpServers",)
    format: str = "json"            # "json" | "toml"
    entry_style: str = "standard"   # "standard" | "vscode"
    note: str = ""


AGENTS: dict[str, AgentSpec] = {
    "zcode": AgentSpec(
        id="zcode",
        name="ZCode",
        global_path="~/.zcode/cli/config.json",
        project_path=".zcode/config.json",
        key_path=("mcp", "servers"),
        note="ZCode also reads .agents/mcp.json as a fallback; .zcode/ takes precedence.",
    ),
    "claude": AgentSpec(
        id="claude",
        name="Claude Code",
        global_path="~/.claude.json",
        project_path=".mcp.json",
        note="Global scope is usually set with 'claude mcp add -s user'; editing "
             "~/.claude.json directly also works.",
    ),
    "cursor": AgentSpec(
        id="cursor",
        name="Cursor",
        global_path="~/.cursor/mcp.json",
        project_path=".cursor/mcp.json",
    ),
    "vscode": AgentSpec(
        id="vscode",
        name="VS Code (Copilot Chat)",
        global_path=str(Path(_appdata("Code", "User", "mcp.json"))),
        project_path=".vscode/mcp.json",
        key_path=("servers",),
        entry_style="vscode",
        note="VS Code uses the 'servers' key and requires an entry with type='stdio'.",
    ),
    "codex": AgentSpec(
        id="codex",
        name="Codex CLI",
        global_path="~/.codex/config.toml",
        project_path=None,
        key_path=("mcp_servers",),
        format="toml",
        note="Codex configures MCP servers globally in ~/.codex/config.toml.",
    ),
    "gemini": AgentSpec(
        id="gemini",
        name="Gemini CLI",
        global_path="~/.gemini/settings.json",
        project_path=".gemini/settings.json",
    ),
    "windsurf": AgentSpec(
        id="windsurf",
        name="Windsurf",
        global_path="~/.codeium/windsurf/mcp_config.json",
        project_path=None,
    ),
    "claude-desktop": AgentSpec(
        id="claude-desktop",
        name="Claude Desktop",
        global_path=str(Path(_appdata("Claude", "claude_desktop_config.json"))),
        project_path=None,
        note="On macOS the file lives at "
             "~/Library/Application Support/Claude/claude_desktop_config.json.",
    ),
    "generic": AgentSpec(
        id="generic",
        name="Generic (print a ready-to-paste snippet)",
        global_path=None,
        project_path=None,
        note="Use --print to get the snippet; paste it into your agent's config.",
    ),
}


def agent_ids() -> list[str]:
    return sorted(AGENTS)


def resolve_path(spec: AgentSpec, scope: str, project_dir: str | Path | None) -> Path:
    """Absolute path of the configuration file for this agent and scope."""
    if scope == "project":
        if not spec.project_path:
            raise ValueError(
                f"{spec.name} has no project-scope MCP configuration; use --scope global."
            )
        base = Path(project_dir) if project_dir else Path.cwd()
        return (base / spec.project_path).resolve()
    if not spec.global_path:
        raise ValueError(
            f"{spec.name} has no known global configuration file; use --print."
        )
    raw = os.path.expandvars(os.path.expanduser(spec.global_path))
    return Path(raw).resolve()


def detect_agents(project_dir: str | Path | None = None) -> list[dict]:
    """Report which agents look installed (their config files exist)."""
    found = []
    for spec in AGENTS.values():
        entry: dict = {"id": spec.id, "name": spec.name}
        for scope in ("global", "project"):
            try:
                path = resolve_path(spec, scope, project_dir)
            except ValueError:
                continue
            entry[f"{scope}_path"] = str(path)
            entry[f"{scope}_exists"] = path.exists()
        found.append(entry)
    return found
