"""Safe configuration writers: JSON deep merge and TOML tables, always with backups."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from .agents import AgentSpec


class InstallError(RuntimeError):
    pass


# ------------------------------------------------------------------ entry build


def build_entry(command_kind: str, repo_url: str, project_dir: Path) -> dict:
    """The MCP server entry written into an agent configuration."""
    if command_kind == "uvx":
        entry = {"command": "uvx", "args": ["--from", repo_url, "comsol-mcp"]}
    elif command_kind == "pip":
        entry = {"command": "comsol-mcp", "args": []}
    elif command_kind == "python":
        entry = {"command": sys.executable, "args": ["-m", "comsol_mcp"]}
    elif command_kind == "local":
        entry = {
            "command": sys.executable,
            "args": ["-m", "comsol_mcp"],
            "env": {"PYTHONPATH": str((project_dir / "src").resolve())},
        }
    else:
        raise InstallError(f"Unknown command kind '{command_kind}'.")
    return entry


def choose_command_kind(requested: str, repo_url: str, project_dir: Path) -> str:
    if requested != "auto":
        return requested
    if repo_url and shutil.which("uvx"):
        return "uvx"
    if (project_dir / "src" / "comsol_mcp").is_dir():
        return "local"
    if shutil.which("comsol-mcp"):
        return "pip"
    return "python"


def with_entry_style(entry: dict, spec: AgentSpec) -> dict:
    if spec.entry_style == "vscode":
        return {"type": "stdio", **entry}
    return entry


# ---------------------------------------------------------------------- JSON


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"{path} is not valid JSON ({exc}). Fix it manually or use --print."
        ) from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object.")
    return data


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    return backup


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _set_nested(data: dict, key_path: tuple[str, ...], name: str, entry: dict) -> None:
    target = data
    for key in key_path:
        value = target.get(key)
        if not isinstance(value, dict):
            value = {}
            target[key] = value
        target = value
    target[name] = entry


# ---------------------------------------------------------------------- TOML


def _write_toml(path: Path, key_path: tuple[str, ...], name: str, entry: dict) -> None:
    try:
        import tomlkit
    except ImportError as exc:  # pragma: no cover
        raise InstallError(
            "The 'tomlkit' package is required to write TOML configuration files."
        ) from exc
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()
    table = doc
    for key in key_path:
        if key not in table:
            table[key] = tomlkit.table()
        table = table[key]
    item = tomlkit.table()
    item["command"] = entry["command"]
    if entry.get("args"):
        item["args"] = entry["args"]
    if entry.get("env"):
        item["env"] = entry["env"]
    table[name] = item
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


# --------------------------------------------------------------------- public


def snippet(spec: AgentSpec, name: str, entry: dict) -> str:
    """Ready-to-paste configuration text."""
    styled = with_entry_style(entry, spec)
    if spec.format == "toml":
        import json as _json
        lines = [f"[{' .'.join(spec.key_path)}.{name}]"]
        lines.append(f'command = "{styled["command"]}"')
        if styled.get("args"):
            lines.append(f'args = {_json.dumps(styled["args"])}')
        if styled.get("env"):
            lines.append(f'env = {_json.dumps(styled["env"])}')
        return "\n".join(lines)
    data: dict = {}
    _set_nested(data, spec.key_path, name, styled)
    return json.dumps(data, indent=2, ensure_ascii=False)


def install(spec: AgentSpec, scope: str, name: str, entry: dict,
            project_dir: Path, dry_run: bool = False) -> dict:
    """Write (or report) the configuration change. Returns a report dict."""
    from .agents import resolve_path

    try:
        path = resolve_path(spec, scope, project_dir)
    except ValueError as exc:
        raise InstallError(str(exc)) from exc

    report: dict = {
        "agent": spec.id,
        "agent_name": spec.name,
        "scope": scope,
        "path": str(path),
        "key_path": ".".join(spec.key_path),
        "entry": with_entry_style(entry, spec),
        "dry_run": dry_run,
        "note": spec.note or None,
    }

    existed = path.exists()
    if spec.format == "json":
        data = _read_json(path)
        _set_nested(data, spec.key_path, name, with_entry_style(entry, spec))
        if dry_run:
            report["action"] = "would update" if existed else "would create"
            report["resulting_file"] = data
            return report
        report["backup"] = str(backup) if (backup := _backup(path)) else None
        _write_json(path, data)
    else:
        if dry_run:
            report["action"] = "would update" if existed else "would create"
            report["snippet"] = snippet(spec, name, entry)
            return report
        report["backup"] = str(backup) if (backup := _backup(path)) else None
        _write_toml(path, spec.key_path, name, entry)

    report["action"] = "updated" if existed else "created"
    return report
