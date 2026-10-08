"""Offline contract + unit tests for the comsol-dsh skill runner.

No COMSOL and no server process needed: the engine's tool registry is read
in-process (build_server() does not start the JVM). From the skill root:
    python -m pytest
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import run_workflow as rw  # noqa: E402


def test_required_tools_cover_every_call_site():
    """Every comsol_* name the runner calls must be declared in REQUIRED_TOOLS."""
    source = (SKILL_ROOT / "scripts" / "run_workflow.py").read_text(encoding="utf-8")
    called = set(re.findall(r'call\([^,]+,\s*"(comsol_[a-z_]+)"', source))
    assert called, "no tool calls found - the scanner broke"
    assert called <= set(rw.REQUIRED_TOOLS), called - set(rw.REQUIRED_TOOLS)


def test_engine_registry_matches_required_tools():
    """Validate against the same engine the runner would spawn."""
    repo = rw.resolve_repo()
    if repo is not None:
        src = str(repo / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
    pytest.importorskip("comsol_mcp")
    from comsol_mcp.server import build_server

    tools = asyncio.run(_list_tools(build_server))
    registry = {t["name"]: set(t["params"]) for t in tools}
    for name, params in rw.REQUIRED_TOOLS.items():
        assert name in registry, f"tool {name} missing"
        missing = [p for p in params if p not in registry[name]]
        assert not missing, f"{name} lacks {missing}"


async def _list_tools(build_server):
    mcp = build_server()
    return [
        {"name": t.name,
         "params": list((t.inputSchema or {}).get("properties", {}))}
        for t in await mcp.list_tools()
    ]


def test_repo_resolution_finds_sibling_checkout(monkeypatch):
    monkeypatch.delenv("COMSOL_MCP_REPO", raising=False)
    repo = rw.resolve_repo()
    assert repo is not None
    assert (repo / "src" / "comsol_mcp" / "__init__.py").is_file()


def test_repo_resolution_env_override_wins(monkeypatch, tmp_path):
    fake = tmp_path / "custom-engine"
    (fake / "src" / "comsol_mcp").mkdir(parents=True)
    (fake / "src" / "comsol_mcp" / "__init__.py").write_text("")
    monkeypatch.setenv("COMSOL_MCP_REPO", str(fake))
    assert rw.resolve_repo() == fake


def test_parse_result():
    assert rw.parse_result('{"a": 1}') == {"a": 1}
    assert rw.parse_result("not json") is None
    assert rw.parse_result("[1,2]") is None


def test_find_solver_feature_prefers_stationary():
    children = [
        {"container": "feature", "tag": "d1", "label": "Dependent Variables"},
        {"container": "feature", "tag": "s1", "label": "Stationary",
         "type": "Stationary",
         "children": [{"container": "feature", "tag": "d2", "label": "x"}]},
    ]
    assert rw.find_solver_feature(children)["tag"] == "s1"


def test_find_solver_feature_falls_back_and_handles_empty():
    children = [{"container": "study", "tag": "std1", "children": [
        {"container": "feature", "tag": "stat", "label": "Stationary"}]}]
    assert rw.find_solver_feature(children)["tag"] == "stat"
    assert rw.find_solver_feature([]) is None
    assert rw.find_solver_feature(None) is None


@pytest.mark.parametrize(
    ("current", "override", "expected"),
    [("3", None, "2"), ("4", None, "3"), ("5", None, "4"),
     ("1", None, "4"), ("weird", None, "4"), ("3", "5", "5"),
     ("3", "none", None), ("3", "keep", None)],
)
def test_pick_hauto(current, override, expected):
    assert rw.pick_hauto(current, override) == expected


def _desc_with_groups(groups):
    return {"results": {"plot_groups": groups}, "components": []}


def test_pick_plot_group_explicit_tag():
    desc = _desc_with_groups([{"tag": "pg1"}, {"tag": "pg3"}])
    assert rw.pick_plot_group(desc, "pg3")[0] == "pg3"


def test_pick_plot_group_temperature_keyword_english():
    desc = _desc_with_groups([{"tag": "pg1", "label": "Mesh 1"},
                              {"tag": "pg2", "label": "Temperature (K)"}])
    assert rw.pick_plot_group(desc, "temperature")[0] == "pg2"


def test_pick_plot_group_temperature_keyword_chinese():
    desc = _desc_with_groups([{"tag": "pg1", "label": "网格"},
                              {"tag": "pg2", "label": "温度分布"}])
    assert rw.pick_plot_group(desc, "temperature")[0] == "pg2"


def test_pick_plot_group_auto_first_group_and_substring():
    desc = _desc_with_groups([{"tag": "pg1", "label": "Velocity"},
                              {"tag": "pg2", "label": "Temperature 2"}])
    assert rw.pick_plot_group(desc, "auto")[0] == "pg1"
    assert rw.pick_plot_group(desc, "unknown-word")[0] == "pg1"


def test_pick_plot_group_no_groups():
    tag, tags = rw.pick_plot_group({"results": {}}, "auto")
    assert tag is None and tags == []


def test_auto_expression_by_physics():
    def desc(types):
        return {"components": [{"physics": [
            {"type": t, "label": ""} for t in types]}]}
    assert rw.auto_expression(desc(["HeatTransfer"])) == "T"
    assert rw.auto_expression(desc(["SolidMechanics"])) == "solid.mises"
    assert rw.auto_expression(desc(["Electrostatics"])) == "es.normE"
    assert rw.auto_expression(desc([])) == "T"
    assert rw.auto_expression({}) == "T"


def test_describe_brief_renders_key_facts():
    desc = json.loads((SKILL_ROOT / "tests" / "fixtures" /
                       "describe_sample.json").read_text(encoding="utf-8"))
    brief = rw.describe_brief(desc)
    assert "busbar_terminal.mph" in brief
    assert "std1" in brief
    assert "4912" in brief


def test_numeric_stats_flattens_nested_values():
    stats = rw.numeric_stats([[300.0, 310.0], [320.0]])
    assert stats == {"min": 300.0, "max": 320.0, "mean": 310.0}
    assert rw.numeric_stats(["a", "b"]) is None


def test_handle_lock_refuses_with_live_comsol(monkeypatch, tmp_path):
    model = tmp_path / "m.mph"
    model.write_bytes(b"x")
    lock = tmp_path / "m.mph.lock"
    lock.write_text("")
    monkeypatch.setattr(rw, "comsol_processes",
                        lambda: ["comsolmphserver.exe"])
    rw.handle_lock(model, force=False)
    assert lock.exists()
    rw.handle_lock(model, force=True)
    assert not lock.exists()


def test_handle_lock_removes_orphan_when_no_comsol(monkeypatch, tmp_path):
    model = tmp_path / "m.mph"
    model.write_bytes(b"x")
    lock = tmp_path / "m.mph.lock"
    lock.write_text("")
    monkeypatch.setattr(rw, "comsol_processes", lambda: [])
    rw.handle_lock(model, force=False)
    assert not lock.exists()


def test_port_busy_detects_free_port():
    assert rw.port_busy(1) is False


def test_set_param_bad_input_aborts_before_any_comsol_call():
    args = _args(set_param=["NOEQ"])
    code = asyncio.run(
        rw.run_workflow(_FakeSession(), args, Path("m.mph")))
    assert code == 4


def _args(**kwargs):
    ns = dict(set_param=None, no_solver_tweak=True, no_mesh=True,
              study=None, dataset=None, expression="T", plot_hint="auto",
              timeout=0, no_close=True, allow_code=False, port=2036)
    ns.update(kwargs)
    return type("A", (), ns)()


class _FakeSession:
    """Minimal stand-in that only answers start/describe/get_parameters."""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        payload = json.dumps({"model": {"label": "m.mph"}, "parameters": {},
                              "components": [], "studies": [],
                              "solutions": [], "results": {}})
        return _FakeResult(payload)


class _FakeResult:
    def __init__(self, text):
        self.isError = False
        self.content = [_FakeText(text)]


class _FakeText:
    def __init__(self, text):
        self.text = text


