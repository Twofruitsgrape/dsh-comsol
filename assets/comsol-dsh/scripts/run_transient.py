#!/usr/bin/env python3
"""comsol-dsh transient driver: time-varying boundary -> Time Dependent study -> solve.

Generalises the one-off busbar transient edit into a reusable workflow:

  open -> snapshot -> add an Analytic time function -> rewire a heat/temperature
  boundary condition to it -> create a Time Dependent study -> (optional) rebuild
  mesh -> solve with live progress -> verify the time series -> save-as -> close.

This is the structural-editing path: creating a function, a study and a solver
sequence is beyond the plain read/write MCP tools, so it runs through the
engine's comsol_run_python escape hatch and therefore REQUIRES --allow-code
(the package hard rule; the engine refuses the code path otherwise).

Field-tested traps this script already handles (COMSOL 6.2, Chinese desktop):
  * Analytic functions take their argument as `x`, NOT `s`/`t` - a body that
    references the wrong name fails deep inside the solve ("Variable: s not
    found"). We build the expression in `x` and call it as Func(t).
  * Units go INSIDE the expression (293.15[K]+...); `funcunit`/`argunit` are
    not valid Analytic properties and setting them raises "Unknown".
  * A failed solve leaves an empty-shell solution (solN) with no datasets;
    stale datasets then break comsol_evaluate ("Dataset does not exist"). We
    remove empty shells before solving and re-derive dataset tags after.
  * In a GUI-owned session you cannot create numerical/plot operations
    ("Operation cannot be created in this context"), so verification uses a
    Data export node (allowed) or a headless reopen, never result().numerical().
  * study.createSolver()/createAutoSequencesAll() do not exist on 6.2; the
    correct call is sol.createAutoSequences(<stepTag>) and even that is
    optional because study.run() auto-builds the sequence.

Exit codes: 0 ok, 2 start failed, 3 solve failed/timeout, 4 bad args/edit/save,
5 port busy, 6 engine contract, 7 unexpected, 8 solve finished but verify failed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import run_workflow as rw  # noqa: E402

POLL_SECONDS = 5.0


def build_function_expr(base: str, delta: str, tau: str) -> str:
    """Analytic body in the argument `x`, units embedded (never funcunit prop)."""
    return f"{base}[K]+{delta}[K]*(1-exp(-x/{tau}[s]))"


def marker(text: str, tag: str):
    inner = text
    try:
        outer = json.loads(text)
        inner = outer.get("output", "") if isinstance(outer, dict) else ""
    except Exception:
        pass
    i = inner.find(tag)
    if i < 0:
        return None
    try:
        val, _ = json.JSONDecoder().raw_decode(inner[i + len(tag):].strip())
        return val
    except Exception as exc:
        print(f"  marker parse failed ({tag}): {exc}", flush=True)
        return None


def edit_code(func_name, expr, bc_feature, bc_prop, study_tag, step_tag, tlist):
    """Python run inside COMSOL (namespace has `model`, `java`, `mph`)."""
    return f'''
import json
m = model.java
log = {{}}
FN = {func_name!r}
if FN in [str(t) for t in m.func().tags()]:
    m.func().remove(FN)
f = m.func().create(FN, "Analytic")
f.set("expr", {expr!r})
ST = {study_tag!r}
if ST in [str(t) for t in m.study().tags()]:
    m.study().remove(ST)
std = m.study().create(ST)
try:
    step = std.create({step_tag!r}, "Transient")
except Exception:
    step = std.create({step_tag!r}, "Time")
step.set("tlist", {tlist!r})
# rewire the boundary condition
comp = m.component("comp1")
target = {bc_feature!r}
prop = {bc_prop!r}
if not target:
    ht = comp.physics("ht")
    for t in ht.feature().tags():
        ft = ht.feature(t)
        ty = str(ft.getType()).lower()
        if "temperature" in ty:
            try:
                ft.getString("T0"); target, prop = t, "T0"; break
            except Exception:
                pass
    if not target:
        for t in ht.feature().tags():
            ft = ht.feature(t)
            if "heatflux" in str(ft.getType()).lower():
                try:
                    ft.getString("Text"); target, prop = t, "Text"; break
                except Exception:
                    pass
if not target:
    raise RuntimeError("no rewirable temperature boundary found")
old = str(comp.physics("ht").feature(target).getString(prop))
comp.physics("ht").feature(target).set(prop, FN + "(t)")
log["bc"] = {{"feature": str(target), "property": prop,
              "old": old, "new": FN + "(t)"}}
log["study"] = ST
log["func"] = FN
print("EDIT:" + json.dumps(log))
'''


def solver_code(study_tag, step_tag, rtol, initstep, maxstep):
    return f'''
import json
m = model.java
info = {{}}
ST = {study_tag!r}
# drop any empty-shell solution left by a previous failed attempt
for s in [str(t) for t in m.sol().tags()]:
    ft = m.sol(s)
    try:
        has = len([str(x) for x in ft.feature().tags()]) > 0
    except Exception:
        has = True
    if not has:
        try:
            m.sol().remove(s)
            info["dropped_empty"] = s
        except Exception:
            pass
# Pre-build the solver sequence so time-solver tolerances can be set. If any
# step of this fails, ROLL BACK the half-built solution: an orphan shell
# strands a junk solN/dsetN in the saved model, and study.run() auto-builds
# the sequence happily on its own.
sol = None
try:
    tag = "sol_" + ST
    if tag in [str(t) for t in m.sol().tags()]:
        m.sol().remove(tag)
    m.sol().create(tag)
    sol = m.sol(tag)
    sol.study(ST)
    built = None
    method = getattr(sol, "createAutoSequences", None)
    if method:
        for arg in (["{step_tag}"], []):
            try:
                method(*arg); built = "createAutoSequences" + str(arg); break
            except Exception:
                continue
    info["auto"] = built or "failed"
    if not built:
        raise RuntimeError("no auto-sequence API usable")
except Exception as e:
    info["prebuilt"] = "rolled back: " + str(e)[:100]
    if sol is not None:
        try:
            m.sol().remove(str(sol.tag())); sol = None
        except Exception:
            pass
if sol is not None:
    info["tag"] = str(sol.tag())
    for t in sol.feature().tags():
        ft = sol.feature(t)
        ty = str(ft.getType())
        if "Time" in ty or "time" in str(t).lower():
            setr = {{}}
            for p, v in (("rtol", {rtol!r}), ("initstep", {initstep!r}),
                         ("maxstep", {maxstep!r})):
                try:
                    ft.set(p, v); setr[p] = v
                except Exception:
                    setr[p] = "skip"
            info["time_solver"] = {{"tag": str(t), "type": ty, "set": setr}}
info["sols_before"] = [str(t) for t in m.sol().tags()]
print("SOLVER:" + json.dumps(info))
'''


def show_code(study_tag, plot_hint):
    """Rebind an existing plot group to the transient dataset and draw it.

    GUI-session safe: only set()/run() on nodes that already exist (the model
    ships with a Temperature plot group). Creating a new plot/numerical op is
    refused in a GUI-owned session, so we reuse and re-point, exactly like a
    user selecting the time-dependent dataset in the desktop and pressing
    Render. Node access uses m.result(tag) - the accessor pattern verified on
    6.2 (collections are not callable)."""
    return f'''
import json
m = model.java
out = {{}}
r = m.result()
ST = {study_tag!r}
sol_study = {{}}
for s in [str(t) for t in m.sol().tags()]:
    got = "?"
    try:
        got = str(m.sol(s).getString("study"))
    except Exception:
        try:
            got = str(m.sol(s).study())
        except Exception:
            pass
    sol_study[s] = got
tds = None
for t in r.dataset().tags():
    try:
        if sol_study.get(str(r.dataset(t).getString("solution"))) == ST:
            tds = str(t); break
    except Exception:
        pass
out["t_dataset"] = tds
from comsol_mcp import model_access as ma
groups = ma.plot_group_tags(m)
out["plot_groups"] = groups
pick = None
hint = {plot_hint!r}
for g in groups:
    if hint and g == hint:
        pick = g; break
    try:
        lbl = str(m.result(g).label())
    except Exception:
        lbl = ""
    if any(k in lbl for k in ("emperature", "温度")):
        pick = g; break
pick = pick or (groups[0] if groups else None)
if pick and tds:
    try:
        pg = m.result(pick)
        pg.set("data", tds)
        pg.run()
        out["shown"] = {{"group": pick, "dataset": tds}}
    except Exception as e:
        out["show_err"] = str(e)[:160]
print("SHOW:" + json.dumps(out))
'''


def verify_code(study_tag, expr, csv_path):
    """Headless-safe verification: Data export node (no numerical op)."""
    return f'''
import json
m = model.java
out = {{}}
r = m.result()
ST = {study_tag!r}
# map each solution tag to the study it is attached to, then pick the dataset
# whose solution belongs to the transient study. Deterministic: survives an
# empty-shell solN left by a failed attempt and does not assume tag order.
sol_study = {{}}
for s in [str(t) for t in m.sol().tags()]:
    node = m.sol(s)
    got = "?"
    for acc in ("study",):
        try:
            got = str(node.getString(acc)); break
        except Exception:
            pass
        try:
            got = str(node.study()); break
        except Exception:
            pass
    sol_study[s] = got
out["sol_study"] = sol_study
ds = {{}}
for t in r.dataset().tags():
    try:
        ds[str(t)] = str(r.dataset(t).getString("solution"))
    except Exception:
        ds[str(t)] = "?"
out["datasets"] = ds
chosen = None
for tag, sol in ds.items():
    if sol_study.get(sol) == ST:
        chosen = tag
        break
# fall back: any dataset on a solution that is neither empty nor the steady one
if chosen is None:
    nonempty = [s for s, st in sol_study.items()
                if st == ST and s in ds.values()]
    chosen = next((k for k, v in ds.items() if v in nonempty), None)
out["t_dataset"] = chosen
out["t_solution"] = ds.get(chosen)
if chosen:
    ex = r.export()
    if "vdtrans" in [str(x) for x in ex.tags()]:
        ex.remove("vdtrans")
    node = ex.create("vdtrans", "Data")
    node.set("data", chosen)
    node.set("expr", [{expr!r}])
    node.set("filename", {csv_path!r})
    try:
        node.run(); out["csv_ok"] = True
    except Exception as e:
        out["csv_err"] = str(e)[:140]
    try:
        ex.remove("vdtrans")
    except Exception:
        pass
print("VERIFY:" + json.dumps(out))
'''


def reshape_timeseries(csv_path):
    """Parse a COMSOL Data-export CSV into per-time-step max/min."""
    rows = []
    with open(csv_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            parts = line.split(",")
            if len(parts) < 4:      # x,y,z + at least one value column
                continue
            try:
                vals = [float(x) for x in parts[3:]]
            except ValueError:
                continue
            rows.append(vals)
    if not rows:
        return None
    ncols = min(len(r) for r in rows)
    rows = [r[:ncols] for r in rows]
    tmax = [max(r[k] for r in rows) for k in range(ncols)]
    tmin = [min(r[k] for r in rows) for k in range(ncols)]
    return {"nodes": len(rows), "steps": ncols, "maxT": tmax, "minT": tmin}


async def amain(args):
    if not args.allow_code:
        print("ABORT: transient editing runs through comsol_run_python and "
              "requires --allow-code (see SKILL.md hard rule 6).", flush=True)
        return 4
    repo = Path(args.mcp_repo).resolve() if args.mcp_repo else rw.resolve_repo()
    if repo is None:
        print("ABORT: comsol-mcp not found.", flush=True)
        return 4
    model = Path(args.model).resolve()
    home = Path(args.home).resolve()
    out = Path(args.save_as)
    out = (out if out.is_absolute() else Path.cwd() / out).resolve()
    if not model.exists():
        print(f"ABORT: model not found: {model}", flush=True)
        return 4
    if out.exists():
        print(f"ABORT: refusing to overwrite {out} (pass a new path).",
              flush=True)
        return 4
    rw.handle_lock(model, force=args.allow_live_lock)
    if rw.port_busy(args.port) and not args.reuse_port:
        print(f"ABORT: port {args.port} busy.", flush=True)
        return 5

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["COMSOL_MCP_HOME"] = str(home)
    env["COMSOL_MCP_LOG"] = "INFO"
    env["COMSOL_MCP_ALLOW_CODE"] = "1"
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "comsol_mcp"],
                                   env=env, cwd=str(repo))
    home.mkdir(parents=True, exist_ok=True)
    engine_log = home / "logs" / f"engine-{args.port}.log"
    engine_log.parent.mkdir(parents=True, exist_ok=True)
    with engine_log.open("a", encoding="utf-8", errors="replace") as errlog:
        async with stdio_client(params, errlog=errlog) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                problems = await rw.check_engine_contract(s)
                if problems:
                    for line in problems:
                        print(f"  contract problem: {line}", flush=True)
                    return 6
                return await drive(s, args, model, out, home)


async def drive(s, args, model, out, home):
    started = False
    code = 7
    try:
        print("\n---- 1. open model ----", flush=True)
        err, _ = await rw.call(s, "comsol_start_session",
                               {"model_path": str(model), "port": args.port},
                               show=200)
        if err:
            print("ABORT: could not open the model. Check COMSOL dialogs / "
                  ".mph.lock (a lock held by YOUR open COMSOL window means the "
                  "file is in use - close it or run on a copy).", flush=True)
            # start_session may still have launched a server + desktop;
            # tear them down so a failed open leaves no orphan processes.
            await rw.cleanup(s, "ending session after failed open")
            return 2
        started = True
        await rw.call(s, "comsol_describe_model", show=0)

        print("\n---- 2. snapshot ----", flush=True)
        await rw.call(s, "comsol_snapshot", {"label": "before-transient"},
                      show=200)

        print("\n---- 3. add time function + transient study + rewire BC ----",
              flush=True)
        expr = build_function_expr(args.base_k, args.delta_k, args.tau_s)
        print(f"  function {args.func} = {expr}", flush=True)
        edit = edit_code(args.func, expr, args.bc_feature, args.bc_prop,
                         args.study, args.step, args.tlist)
        err, txt = await rw.call(s, "comsol_run_python", {"code": edit},
                                 show=500)
        info = marker(txt, "EDIT:")
        if err or not info:
            print("ABORT: model edit failed.", flush=True)
            return 4
        bc = info.get("bc", {})
        print(f"  BC {bc.get('feature')}.{bc.get('property')}: "
              f"{bc.get('old')} -> {bc.get('new')}", flush=True)

        print("\n---- 4. solver setup ----", flush=True)
        err, txt = await rw.call(s, "comsol_run_python",
                                 {"code": solver_code(args.study, args.step,
                                                      args.rtol, args.initstep,
                                                      args.maxstep)},
                                 show=500)
        print("  solver:", json.dumps(marker(txt, "SOLVER:"),
                                      ensure_ascii=False), flush=True)

        if not args.no_mesh:
            print("\n---- 5. mesh rebuild ----", flush=True)
            err, _ = await rw.call(s, "comsol_mesh",
                                   {"action": "build_stats"}, show=300)
            if err:
                print("ABORT: mesh build failed.", flush=True)
                return 4

        print(f"\n---- 6. transient solve {args.study} ----", flush=True)
        # Like a manual COMSOL run: show COMSOL's own progress window while it
        # computes (the sweeper is told to leave titled dialogs alone in this
        # mode). --stream-progress keeps the console-side stream instead.
        if not args.stream_progress:
            await rw.call(s, "comsol_set_progress_mode", {"mode": "desktop"},
                          show=200)
        err, _ = await rw.call(s, "comsol_solve_start",
                               {"study": args.study}, show=300)
        if err:
            print("ABORT: solve start failed.", flush=True)
            return 3
        deadline = time.time() + args.timeout
        last, state = "", "solving"
        while time.time() < deadline:
            err, stxt = await rw.call(s, "comsol_solve_status", show=0)
            st = rw.parse_result(stxt) or {}
            state = st.get("state", "idle")
            prog = st.get("progress") or {}
            line = " | ".join(str(prog[k]) for k in ("percent", "stage",
                                                     "last_line")
                              if prog.get(k) is not None)
            if line and line != last:
                print(f"   [{state}] {line[:130]}", flush=True)
                last = line
            if state in ("done", "failed"):
                break
            await asyncio.sleep(POLL_SECONDS)
        if state != "done":
            print(f"ABORT: solve state={state}", flush=True)
            return 3

        print("\n---- 7. verify time series ----", flush=True)
        # back to the file-progress channel before any further server work
        if not args.stream_progress:
            await rw.call(s, "comsol_set_progress_mode", {"mode": "stream"},
                          show=200)
        csv_path = home / "exports" / "T_timeseries.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        err, txt = await rw.call(s, "comsol_run_python",
                                 {"code": verify_code(args.study, args.expr,
                                                      str(csv_path).replace("\\", "/"))},
                                 show=400)
        vinfo = marker(txt, "VERIFY:") or {}
        print("  datasets:", json.dumps(vinfo.get("datasets"),
                                        ensure_ascii=False), flush=True)
        print(f"  transient dataset: {vinfo.get('t_dataset')} "
              f"(solution {vinfo.get('t_solution')})", flush=True)
        verified = False
        if vinfo.get("csv_ok") and csv_path.exists():
            series = reshape_timeseries(str(csv_path))
            if series:
                verified = True
                mx, mn = series["maxT"], series["minT"]
                print(f"  {series['nodes']} nodes x {series['steps']} steps",
                      flush=True)
                print("     t(s)   maxT(K)   minT(K)", flush=True)
                for k in list(range(0, series["steps"], 10)) \
                        + [series["steps"] - 1]:
                    print(f"    {k:4d}   {mx[k]:7.2f}   {mn[k]:7.2f}",
                          flush=True)
                print(f"  rise: maxT {mx[0]:.2f}K -> {mx[-1]:.2f}K "
                      f"(+{mx[-1]-mx[0]:.2f}K)", flush=True)
        else:
            print("  (no CSV; verification limited to solve success)",
                  flush=True)

        print("\n---- 8. save solved model ----", flush=True)
        err, txt = await rw.call(s, "comsol_save_model", {"path": str(out)},
                                 show=300)
        if err:
            print("ABORT: save failed.", flush=True)
            return 4
        print("  saved ->", (rw.parse_result(txt) or {}).get("saved_to"),
              flush=True)
        code = 0 if verified else 8

        print("\n---- 9. load results into the desktop ----", flush=True)
        err, txt = await rw.call(s, "comsol_run_python",
                                 {"code": show_code(args.study, args.plot_hint)},
                                 show=400)
        sh = marker(txt, "SHOW:") or {}
        if sh.get("shown"):
            print(f"  plot {sh['shown']['group']} now shows {sh['shown']['dataset']} "
                  "(time-dependent) - the COMSOL window renders it", flush=True)
        elif sh.get("show_err"):
            print(f"  could not re-point the plot: {sh['show_err']}", flush=True)
        await rw.call(s, "comsol_focus_desktop", show=200)

        if args.close:
            print("\n---- 10. clean shutdown ----", flush=True)
            await rw.call(s, "comsol_end_session",
                          {"save": False, "stop_server": True,
                           "close_gui": True},
                          show=800)
            started = False
            for _ in range(10):
                if not rw.comsol_processes():
                    break
                await asyncio.sleep(1.0)
            rw.handle_lock(model, force=False)
            print("shutdown verified" if not rw.comsol_processes()
                  else "NOTE: a COMSOL window is still open - close it from its UI",
                  flush=True)
        else:
            # The manual-COMSOL experience: results are on screen, the session
            # stays alive while the user inspects them, and closes itself once
            # the user closes the COMSOL window (or after --hold-max seconds).
            print("\nResults are on screen in the COMSOL window. Inspect them "
                  "and close the window when done - this run then cleans up "
                  f"and exits (max {args.hold_max}s).", flush=True)
            deadline = time.time() + args.hold_max
            while time.time() < deadline:
                err, stxt = await rw.call(s, "comsol_session_status", show=0)
                gui_alive = bool((rw.parse_result(stxt) or {}).get("gui_pid"))
                if not gui_alive:
                    print("desktop closed by user.", flush=True)
                    break
                await asyncio.sleep(2.0)
            else:
                print("hold timeout; closing the desktop gracefully.",
                      flush=True)
            await rw.call(s, "comsol_end_session",
                          {"save": False, "stop_server": True,
                           "close_gui": True},
                          show=800)
            started = False
            for _ in range(10):
                if not rw.comsol_processes():
                    break
                await asyncio.sleep(1.0)
            rw.handle_lock(model, force=False)
            print("shutdown verified" if not rw.comsol_processes()
                  else "NOTE: a COMSOL window is still open - close it from its UI",
                  flush=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        code = 7
    finally:
        if started:
            await rw.cleanup(s, "ending transient session after early exit")
    return code


def _port_owner_pid(port: int) -> int | None:
    """PID listening on `port` (Windows netstat), or None."""
    import subprocess

    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[0] == "TCP" and cols[1].endswith(
                f":{port}") and cols[3].upper() == "LISTENING":
            try:
                return int(cols[4])
            except ValueError:
                return None
    return None


def shutdown_only(port: int) -> int:
    """Close a session left open by a previous keep-open run.

    Attaches to the server on `port`, closes the COMSOL desktop gracefully
    (save dialog answered No; snapshots cover the work), then stops the
    headless server process by its exact port-owner PID and sweeps locks.
    Never force-kills a desktop window - only the server, after it has no
    clients left.
    """
    print(f"--shutdown-only: attaching to port {port}", flush=True)
    if not rw.port_busy(port):
        print("nothing is listening; already down.", flush=True)
        return 0
    env = dict(os.environ)
    env["COMSOL_MCP_LOG"] = "WARNING"
    repo = rw.resolve_repo()
    if repo:
        env["PYTHONPATH"] = str(repo / "src")
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "comsol_mcp"], env=env,
                                   cwd=str(repo) if repo else None)

    async def run():
        with open(os.devnull, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (rd, wr):
                async with ClientSession(rd, wr) as s:
                    await s.initialize()
                    await rw.call(s, "comsol_attach_session",
                                  {"port": port, "gui": False}, show=300)
                    err, txt = await rw.call(
                        s, "comsol_end_session",
                        {"save": False, "stop_server": False,
                         "close_gui": True}, show=900)
        return err

    try:
        end_err = asyncio.run(run())
    except Exception as exc:
        print(f"attach/end failed: {exc}", flush=True)
        end_err = True
    # the desktop is gone (or was never ours); stop the orphaned server by pid
    import subprocess

    pid = _port_owner_pid(port)
    if pid:
        out = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                             capture_output=True, text=True,
                             timeout=30).stdout.strip()
        print(f"server pid {pid}: {out.splitlines()[-1] if out else 'killed'}",
              flush=True)
    for _ in range(10):
        if not rw.comsol_processes():
            break
        time.sleep(1.0)
    print("shutdown complete" if not rw.comsol_processes()
          else "some COMSOL processes remain (a window may need your click)",
          flush=True)
    return 0 if (not end_err and not rw.comsol_processes()) else 1


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--model", help="source .mph to open")
    p.add_argument("--home", help="work dir for logs/exports")
    p.add_argument("--save-as",
                   help="output path for the solved transient model (no overwrite)")
    p.add_argument("--allow-code", action="store_true",
                   help="required: transient editing uses comsol_run_python")
    p.add_argument("--func", default="Tbc", help="Analytic function tag")
    p.add_argument("--base-k", default="293.15", help="start temperature [K]")
    p.add_argument("--delta-k", default="60",
                   help="temperature rise at infinity [K]")
    p.add_argument("--tau-s", default="15", help="rise time constant [s]")
    p.add_argument("--bc-feature", default=None,
                   help="boundary feature tag (default: auto-detect T0 then Text)")
    p.add_argument("--bc-prop", default=None,
                   help="property to rewire (T0/Text); default with --bc-feature")
    p.add_argument("--study", default="std2", help="new transient study tag")
    p.add_argument("--step", default="time", help="transient step tag")
    p.add_argument("--tlist", default="range(0,1,60)",
                   help="COMSOL time list expression")
    p.add_argument("--expr", default="T", help="field to verify/export")
    p.add_argument("--rtol", default="1e-4", help="time solver relative tolerance")
    p.add_argument("--initstep", default="0.05", help="initial time step [s]")
    p.add_argument("--maxstep", default="2", help="max time step [s]")
    p.add_argument("--no-mesh", action="store_true", help="skip mesh rebuild")
    p.add_argument("--timeout", type=int, default=900, help="solve poll deadline [s]")
    p.add_argument("--plot-hint", default=None,
                   help="plot group tag to load with the transient result "
                        "(default: the first group whose label mentions "
                        "temperature/温度)")
    p.add_argument("--stream-progress", action="store_true",
                   help="keep progress in the console stream instead of "
                        "COMSOL's own desktop progress window")
    p.add_argument("--close", action="store_true",
                   help="shut the session down right after saving instead of "
                        "holding the window open for inspection")
    p.add_argument("--hold-max", type=int, default=1800,
                   help="seconds to keep the session open for inspection "
                        "(ends early as soon as you close the COMSOL window)")
    p.add_argument("--port", type=int, default=2036)
    p.add_argument("--reuse-port", action="store_true")
    p.add_argument("--allow-live-lock", action="store_true")
    p.add_argument("--mcp-repo", default=None)
    p.add_argument("--shutdown-only", action="store_true",
                   help="do not run a workflow: just close a session left "
                        "open by an earlier --keep run (closes the desktop "
                        "gracefully, then stops the server on --port)")
    args = p.parse_args()
    if args.shutdown_only:
        return shutdown_only(args.port)
    for req in ("model", "home", "save_as"):
        if not getattr(args, req):
            p.error(f"--{req.replace('_', '-')} is required "
                    "(unless --shutdown-only)")
    if args.bc_feature and not args.bc_prop:
        p.error("--bc-feature requires --bc-prop (T0 or Text)")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
