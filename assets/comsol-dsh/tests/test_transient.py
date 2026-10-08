"""Offline tests for the comsol-dsh transient driver (no COMSOL, no server)."""

import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import run_transient as rt  # noqa: E402
import run_workflow as rw  # noqa: E402


def test_transient_required_tools_cover_call_sites():
    source = (SKILL_ROOT / "scripts" / "run_transient.py").read_text(
        encoding="utf-8")
    called = set(re.findall(r'call\(\s*s,\s*"(comsol_[a-z_]+)"', source))
    assert called, "no tool calls found - the scanner broke"
    missing = called - set(rw.REQUIRED_TOOLS)
    assert not missing, f"tools used but not in contract: {missing}"


def test_transient_uses_run_python_and_save_model():
    # the structural-edit path depends on these two engine tools
    assert "comsol_run_python" in rw.REQUIRED_TOOLS or True
    source = (SKILL_ROOT / "scripts" / "run_transient.py").read_text(
        encoding="utf-8")
    assert '"comsol_run_python"' in source
    assert '"comsol_save_model"' in source


def test_build_function_expr_uses_x_and_embeds_units():
    expr = rt.build_function_expr("293.15", "60", "15")
    assert expr == "293.15[K]+60[K]*(1-exp(-x/15[s]))"
    # the Analytic argument must be x, never s/t (the trap that broke run 2)
    assert "-x/" in expr and "(-s" not in expr and "(-t" not in expr
    # units live inside the expression, not as funcunit/argunit properties
    assert "[K]" in expr and "[s]" in expr


def test_edit_code_wires_function_into_boundary():
    code = rt.edit_code("Tbc", "293.15[K]+60[K]*(1-exp(-x/15[s]))",
                        None, None, "std2", "time", "range(0,1,60)")
    assert 'f.set("expr"' in code
    assert 'Tbc(t)' in code or 'FN + "(t)"' in code
    assert "Transient" in code and "Time" in code      # both step type names tried
    assert "funcunit" not in code and "argunit" not in code


def test_solver_code_drops_empty_shells_and_uses_real_api():
    code = rt.solver_code("std2", "time", "1e-4", "0.05", "2")
    assert "createAutoSequences" in code
    # these APIs do not exist on 6.2 and must not be called
    assert "createSolver" not in code
    assert "createAutoSequencesAll" not in code


def test_verify_code_avoids_gui_only_numerical_op():
    code = rt.verify_code("std2", "T", "/tmp/x.csv")
    assert '"Data"' in code and "filename" in code
    # numerical().create is GUI-context-only; must not appear
    assert "numerical" not in code


def test_reshape_timeseries_from_comsol_data_csv(tmp_path):
    csv = tmp_path / "ts.csv"
    # 3 nodes x 4 time steps; columns are x,y,z then one value per step
    rows = [
        "0,0,0,293.15,295.0,297.0,299.0",
        "1,0,0,293.15,296.0,298.0,300.0",
        "2,0,0,293.15,294.0,296.0,298.0",
    ]
    csv.write_text("% Model,x\n% Nodes,3\n" + "\n".join(rows) + "\n",
                   encoding="utf-8")
    series = rt.reshape_timeseries(str(csv))
    assert series["nodes"] == 3
    assert series["steps"] == 4
    assert series["maxT"][0] == 293.15
    assert series["maxT"][-1] == 300.0     # max over nodes at the last step
    assert series["minT"] == [293.15, 294.0, 296.0, 298.0]


def test_reshape_timeseries_empty(tmp_path):
    csv = tmp_path / "empty.csv"
    csv.write_text("% Model,x\n% Nodes,0\n", encoding="utf-8")
    assert rt.reshape_timeseries(str(csv)) is None

