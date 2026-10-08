"""Unit tests for the agent installers: paths, merge behaviour, formats."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comsol_mcp.installer import writer
from comsol_mcp.installer.agents import AGENTS, detect_agents, resolve_path

COMSOL_ENTRY = {"command": "comsol-mcp", "args": []}


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return tmp_path


def test_project_scope_writes_zcode_config(project: Path):
    report = writer.install(AGENTS["zcode"], "project", "comsol", COMSOL_ENTRY, project)
    path = project / ".zcode" / "config.json"
    assert report["action"] == "created"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcp": {"servers": {"comsol": {"command": "comsol-mcp", "args": []}}}
    }


def test_merge_preserves_existing_content_and_backs_up(project: Path):
    path = project / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "extra": 1}),
                    encoding="utf-8")
    report = writer.install(AGENTS["claude"], "project", "comsol", COMSOL_ENTRY, project)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["extra"] == 1
    assert set(data["mcpServers"]) == {"other", "comsol"}
    assert report["action"] == "updated"
    assert report["backup"] and Path(report["backup"]).exists()


def test_install_is_idempotent(project: Path):
    writer.install(AGENTS["cursor"], "project", "comsol", COMSOL_ENTRY, project)
    writer.install(AGENTS["cursor"], "project", "comsol",
                   {"command": "comsol-mcp", "args": ["--x"]}, project)
    data = json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["comsol"]["args"] == ["--x"]


def test_vscode_uses_servers_key_and_stdio_type(project: Path):
    writer.install(AGENTS["vscode"], "project", "comsol", COMSOL_ENTRY, project)
    data = json.loads((project / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert data["servers"]["comsol"]["type"] == "stdio"
    assert data["servers"]["comsol"]["command"] == "comsol-mcp"


def test_codex_writes_toml_table(project: Path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(project))
    monkeypatch.setenv("HOME", str(project))
    report = writer.install(AGENTS["codex"], "global", "comsol",
                            {"command": "comsol-mcp", "args": ["--y"],
                             "env": {"COMSOL_MCP_HOME": "C:/tmp"}}, project)
    text = Path(report["path"]).read_text(encoding="utf-8")
    assert "[mcp_servers.comsol]" in text
    assert 'command = "comsol-mcp"' in text
    assert '"--y"' in text

    # second write must keep the document parseable and update in place
    writer.install(AGENTS["codex"], "global", "comsol", COMSOL_ENTRY, project)
    text = Path(report["path"]).read_text(encoding="utf-8")
    assert text.count("[mcp_servers.comsol]") == 1


def test_project_scope_rejected_when_unsupported():
    with pytest.raises(ValueError, match="global"):
        resolve_path(AGENTS["codex"], "project", Path.cwd())


def test_print_snippet_shapes(project: Path):
    json_snippet = writer.snippet(AGENTS["zcode"], "comsol", COMSOL_ENTRY)
    assert json.loads(json_snippet)["mcp"]["servers"]["comsol"]["command"] == "comsol-mcp"

    toml_snippet = writer.snippet(AGENTS["codex"], "comsol", COMSOL_ENTRY)
    assert toml_snippet.startswith("[mcp_servers.comsol]")

    vscode_snippet = writer.snippet(AGENTS["vscode"], "comsol", COMSOL_ENTRY)
    assert json.loads(vscode_snippet)["servers"]["comsol"]["type"] == "stdio"


def test_choose_command_kind_prefers_local_checkout(tmp_path: Path):
    (tmp_path / "src" / "comsol_mcp").mkdir(parents=True)
    assert writer.choose_command_kind("auto", "", tmp_path) == "local"
    assert writer.choose_command_kind("auto", "https://example.com/x.git", tmp_path) in (
        "uvx", "local")  # uvx only when uvx is installed
    assert writer.choose_command_kind("pip", "", tmp_path) == "pip"


def test_local_entry_sets_pythonpath(tmp_path: Path):
    entry = writer.build_entry("local", "", tmp_path)
    assert entry["args"] == ["-m", "comsol_mcp"]
    assert entry["env"]["PYTHONPATH"] == str((tmp_path / "src").resolve())


def test_detect_agents_reports_existence(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text("{}", encoding="utf-8")
    report = {item["id"]: item for item in detect_agents(tmp_path)}
    assert report["claude"]["project_exists"] is True
    assert report["cursor"]["project_exists"] is False
    assert "global_path" in report["zcode"]


def test_dry_run_writes_nothing(project: Path):
    report = writer.install(AGENTS["gemini"], "project", "comsol", COMSOL_ENTRY, project,
                            dry_run=True)
    assert report["action"] == "would create"
    assert not (project / ".gemini" / "settings.json").exists()
