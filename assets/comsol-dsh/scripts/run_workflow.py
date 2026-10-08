#!/usr/bin/env python3
"""comsol-dsh skill runner: one command for a full COMSOL workflow.

open -> describe -> (parameters) -> solver tweak -> mesh refine + rebuild ->
background solve with live progress -> evaluate -> export plot image ->
show in the desktop -> clean shutdown.

The engine is comsol-mcp (a stdio MCP server driving a COMSOL Multiphysics
server + visible desktop through MPh). This runner talks to it only through
public MCP tools: no arbitrary code runs against COMSOL unless you pass
--allow-code. The engine is located via COMSOL_MCP_REPO, a sibling checkout,
or an installed comsol-mcp package, in that order.

Exit codes: 0 ok, 2 start_session failed, 3 solve failed/timeout,
4 bad arguments / preflight, 5 preflight: port busy, 6 engine contract,
7 unexpected error, 8 solve timeout (post-processing still ran).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SKILL_DIR = Path(__file__).resolve().parents[1]
POLL_SECONDS = 5.0

# Every tool this runner calls, and the arguments it must accept. Checked
# against the live tool registry before any work starts, so an engine
# version mismatch fails here instead of mid-workflow.
REQUIRED_TOOLS = {
    "comsol_start_session": ["model_path", "port"],
    "comsol_attach_session": ["port", "gui"],
    "comsol_session_status": [],
    "comsol_describe_model": [],
    "comsol_get_parameters": [],
    "comsol_set_parameters": ["parameters"],
    "comsol_snapshot": [],
    "comsol_node_tree": ["path", "depth"],
    "comsol_get_property": ["node"],
    "comsol_set_property": ["node", "property_name", "value"],
    "comsol_mesh": ["action"],
    "comsol_solve_start": [],
    "comsol_solve_status": [],
    "comsol_evaluate": ["expression"],
    "comsol_export_plot_image": ["plot_group"],
    "comsol_show_results": ["plot_group"],
    "comsol_save_model": ["path"],
    "comsol_run_python": ["code"],
    "comsol_set_progress_mode": ["mode"],
    "comsol_focus_desktop": [],
    "comsol_end_session": ["save", "stop_server", "close_gui"],
}


def resolve_repo() -> Path | None:
    """Locate the comsol-mcp checkout.

    Order: COMSOL_MCP_REPO env var, the engine vendored inside the installed
    plugin bundle (`assets/engine`, a sibling of this skill folder), a sibling
    `comsol-mcp` checkout (development layout), then an installed package.
    """
    candidates = []
    if os.environ.get("COMSOL_MCP_REPO"):
        candidates.append(Path(os.environ["COMSOL_MCP_REPO"]))
    candidates.append(SKILL_DIR.parent / "engine")
    candidates.append(SKILL_DIR.parent / "comsol-mcp")
    try:
        spec = importlib.util.find_spec("comsol_mcp")
        if spec and spec.origin:
            origin = Path(spec.origin).resolve()
            for parent in origin.parents[:3]:
                candidates.append(parent)
    except (ImportError, ValueError):
        pass
    for repo in candidates:
        if (repo / "src" / "comsol_mcp" / "__init__.py").exists():
            return repo
    return None


def comsol_processes() -> list[str]:
    """Names of running COMSOL server/desktop processes (Windows)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return []
    names = set()
    for line in out.splitlines():
        name = line.split('","')[0].strip('"').lower()
        if name.startswith("comsol"):
            names.add(name)
    return sorted(names)


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def handle_lock(model: Path, force: bool) -> None:
    """Remove an orphaned <model>.mph.lock, but never while COMSOL is live."""
    lock = model.with_name(model.name + ".lock")
    if not lock.exists():
        return
    procs = comsol_processes()
    if procs and not force:
        print(f"NOTE: {lock.name} exists and COMSOL is running {procs}; "
              "leaving the lock alone (it may be a live session).", flush=True)
        return
    lock.unlink()
    print(f"removed stale lock {lock}", flush=True)


def parse_result(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def text_of(result) -> str:
    blocks = getattr(result, "content", None) or []
    return "\n".join(b.text for b in blocks if getattr(b, "text", None))


async def call(session, name, args=None, show=2000):
    started = time.time()
    result = await session.call_tool(name, args or {})
    text = text_of(result)
    error = bool(getattr(result, "isError", False))
    print(f"\n=== {name} ({time.time() - started:.1f}s) "
          f"[{'!! ERROR' if error else 'ok'}] ===", flush=True)
    print(text[:show], flush=True)
    return error, text


def find_solver_feature(children) -> dict | None:
    """Depth-first search under solution/<tag> for the solver feature.

    Prefers the classic Stationary feature (tag "s1"), else the first
    feature-container child. Mirrors the manual playbook: read the tree,
    never guess node paths.
    """
    queue, fallback = list(children or []), None
    while queue:
        node = queue.pop(0)
        if not isinstance(node, dict):
            continue
        if node.get("container") == "feature" and node.get("tag"):
            if node.get("tag") == "s1":
                return node
            fallback = fallback or node
        queue.extend(node.get("children") or [])
    return fallback


HAUTO_FINER = {"5": "4", "4": "3", "3": "2", "2": "1"}


def pick_hauto(current, override=None):
    """One step finer by default; override "none" keeps mesh untouched."""
    if override is not None and str(override).strip().lower() in ("none", "keep"):
        return None
    if override is not None:
        return str(override).strip()
    return HAUTO_FINER.get(str(current).strip(), "4")


def pick_plot_group(desc: dict, hint: str):
    """Tag of the plot group to export, from comsol_describe_model output."""
    groups = (desc.get("results") or {}).get("plot_groups") or []
    tags = [g.get("tag") for g in groups if g.get("tag")]
    if not tags:
        return None, []
    hint = (hint or "auto").strip()
    if hint != "auto" and hint in tags:
        return hint, tags
    needles = {
        "temperature": ("emperature", "\u6e29\u5ea6", "heat"),
        "stress": ("tress", "mises", "\u5e94\u529b"),
        "velocity": ("eloci", "\u901f\u5ea6"),
    }.get(hint.lower())
    if needles:
        for g in groups:
            label = str(g.get("label", ""))
            if any(n in label for n in needles):
                return g.get("tag"), tags
    low = hint.lower()
    if low not in ("auto", ""):
        for g in groups:
            if low in str(g.get("label", "")).lower():
                return g.get("tag"), tags
    return tags[0], tags


def auto_expression(desc: dict) -> str:
    """Post-processing variable matching the dominant physics."""
    blob = " ".join(
        f"{ph.get('type', '')} {ph.get('label', '')}"
        for comp in desc.get("components") or []
        for ph in comp.get("physics") or []
    ).lower()
    if "heattransfer" in blob:
        return "T"
    if "solidmechanics" in blob:
        return "solid.mises"
    if "electrostatic" in blob:
        return "es.normE"
    return "T"


def describe_brief(desc: dict) -> str:
    parts = []
    model = desc.get("model") or {}
    if model.get("label"):
        parts.append(f"model={model['label']}")
    params = desc.get("parameters") or {}
    if params:
        parts.append(f"parameters={params.get('count')}")
    studies = [s.get("tag") for s in desc.get("studies") or []]
    if studies:
        parts.append(f"studies={studies}")
    sols = [s.get("tag") for s in desc.get("solutions") or []]
    if sols:
        parts.append(f"solutions={sols}")
    for comp in desc.get("components") or []:
        for mesh in comp.get("mesh") or []:
            elems = (mesh.get("statistics") or {}).get("NumElem")
            if elems is not None:
                parts.append(f"mesh {comp.get('tag')}/{mesh.get('tag')}={elems} elems")
    return "  ".join(parts)


def numeric_stats(values) -> dict | None:
    flat = []

    def walk(data):
        if isinstance(data, (list, tuple)):
            for item in data:
                walk(item)
        elif isinstance(data, (int, float)):
            flat.append(float(data))

    walk(values)
    if not flat:
        return None
    return {"min": min(flat), "max": max(flat), "mean": sum(flat) / len(flat)}


async def check_engine_contract(session) -> list[str]:
    """Compare live tool registry against REQUIRED_TOOLS; return problems."""
    tools = await session.list_tools()
    problems = []
    registry = {t.name: set((t.inputSchema or {}).get("properties", {}).keys())
                for t in tools.tools}
    print(f"engine tools: {len(registry)}", flush=True)
    for name, params in sorted(REQUIRED_TOOLS.items()):
        if name not in registry:
            problems.append(f"missing tool {name}")
            continue
        missing = [p for p in params if p not in registry[name]]
        if missing:
            problems.append(f"{name} lacks parameter(s) {missing}")
    return problems


async def cleanup(session, note: str) -> None:
    print(f"\ncleanup: {note}", flush=True)
    try:
        await call(session, "comsol_end_session",
                   {"save": False, "stop_server": True, "close_gui": True},
                   show=1200)
    except Exception as exc:
        print(f"cleanup failed: {exc}", flush=True)


async def amain(args) -> int:
    repo = Path(args.mcp_repo).resolve() if args.mcp_repo else resolve_repo()
    if repo is None:
        print("ABORT: comsol-mcp not found. Set COMSOL_MCP_REPO, keep a "
              "sibling comsol-mcp checkout, or pip install -e it.", flush=True)
        return 4
    model = Path(args.model).resolve()
    home = Path(args.home).resolve()
    print(f"engine repo: {repo}", flush=True)

    if not model.exists():
        print(f"ABORT: model not found: {model}", flush=True)
        return 4
    handle_lock(model, force=args.allow_live_lock)

    if port_busy(args.port) and not args.reuse_port:
        print(f"ABORT: port {args.port} is already listening. One workflow per "
              "port/desktop: stop the other session, or pass --reuse-port to "
              "attach to the server already there.", flush=True)
        return 5

    server_env = dict(os.environ)
    server_env["PYTHONPATH"] = os.pathsep.join(
        [str(repo / "src"), server_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    server_env["COMSOL_MCP_HOME"] = str(home)
    server_env["COMSOL_MCP_LOG"] = "INFO"
    if args.allow_code:
        server_env["COMSOL_MCP_ALLOW_CODE"] = "1"

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "comsol_mcp"],
        env=server_env, cwd=str(repo),
    )
    # Engine INFO logs and any interpreter teardown noise go to a file, not
    # into the run console (older engine builds print an mph atexit traceback
    # when the stdio pipe is torn down).
    engine_log = home / "logs" / f"engine-{args.port}.log"
    engine_log.parent.mkdir(parents=True, exist_ok=True)
    with engine_log.open("a", encoding="utf-8", errors="replace") as errlog:
        code = await _session_loop(params, args, model, errlog)
    if code != 0:
        print(f"(engine log: {engine_log})", flush=True)
    return code


async def _session_loop(params, args, model: Path, errlog) -> int:
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            problems = await check_engine_contract(session)
            if problems:
                for line in problems:
                    print(f"  contract problem: {line}", flush=True)
                print("ABORT: engine does not match this workflow.", flush=True)
                return 6
            return await run_workflow(session, args, model)


async def run_workflow(session, args, model: Path) -> int:
    started_session = False
    code = 7
    try:
        print("\n---- 1. open model ----", flush=True)
        error, text = await call(session, "comsol_start_session",
                                 {"model_path": str(model), "port": args.port},
                                 show=600)
        if error:
            print("\nABORT: could not open the model. Check COMSOL dialogs / "
                  ".mph.lock, then rerun.", flush=True)
            return 2
        started_session = True

        error, desc_text = await call(session, "comsol_describe_model", show=300)
        desc = parse_result(desc_text) or {}
        print("  " + describe_brief(desc), flush=True)

        print("\n---- 2. parameters ----", flush=True)
        error, ptext = await call(session, "comsol_get_parameters", show=300)
        wanted = {}
        for item in args.set_param or []:
            name, sep, value = item.partition("=")
            if not sep or not name.strip() or not value.strip():
                print(f"ABORT: --set-param expects NAME=VALUE, got {item!r}",
                      flush=True)
                return 4
            wanted[name.strip()] = value.strip()
        if wanted:
            print(f"  setting {wanted}", flush=True)
            error, _ = await call(session, "comsol_set_parameters",
                                  {"parameters": wanted}, show=800)
            if error:
                print("ABORT: parameter change failed.", flush=True)
                return 4
        else:
            await call(session, "comsol_snapshot",
                       {"label": "before-solve-no-param-change"}, show=300)

        print("\n---- 3. solver probe / tweak ----", flush=True)
        solver_node = None
        for sol in desc.get("solutions") or []:
            stag = sol.get("tag")
            if not stag:
                continue
            error, ttext = await call(session, "comsol_node_tree",
                                      {"path": f"solution/{stag}", "depth": 4},
                                      show=200)
            if error:
                continue
            tree = parse_result(ttext) or {}
            feature = find_solver_feature(tree.get("children"))
            if feature:
                solver_node = f"solution/{stag}/feature/{feature['tag']}"
                print(f"  solver node: {solver_node} "
                      f"({feature.get('type') or feature.get('label')})",
                      flush=True)
                break
        if solver_node and not args.no_solver_tweak:
            error, gtext = await call(session, "comsol_get_property",
                                      {"node": solver_node}, show=200)
            props = (parse_result(gtext) or {}).get("properties") or {}
            prop, new_value = None, None
            if "stol" in props:
                prop, new_value = "stol", "1.0E-4"
            elif "plot" in props:
                prop = "plot"
                current = str((props["plot"] or {}).get("value", "on"))
                new_value = "off" if current == "on" else "on"
            if prop:
                await call(session, "comsol_set_property",
                           {"node": solver_node, "property_name": prop,
                            "value": new_value}, show=400)
            else:
                print(f"  no known solver property on {solver_node}; "
                      "skipping tweak", flush=True)
        elif not solver_node:
            print("  solver node not found; skipping tweak", flush=True)

        print("\n---- 4. mesh ----", flush=True)
        comp_tag, mesh_tag = None, None
        for comp in desc.get("components") or []:
            meshes = comp.get("mesh") or []
            if meshes and meshes[0].get("tag"):
                comp_tag, mesh_tag = comp.get("tag"), meshes[0].get("tag")
                break
        if args.no_mesh or comp_tag is None:
            print("  mesh step skipped", flush=True)
        else:
            size_node = f"component/{comp_tag}/mesh/{mesh_tag}/feature/size"
            error, htext = await call(session, "comsol_get_property",
                                      {"node": size_node, "property": "hauto"},
                                      show=300)
            current = None
            if not error:
                entry = (parse_result(htext) or {}).get("hauto") or {}
                current = entry.get("value")
            target = pick_hauto(current, None if args.no_mesh else args.hauto)
            if error:
                print("  no hauto on this mesh (non-COMSOL mesh or custom "
                      "size); rebuilding as-is", flush=True)
            elif target is None:
                print(f"  hauto kept at {current}", flush=True)
            else:
                print(f"  hauto {current} -> {target}", flush=True)
                await call(session, "comsol_set_property",
                           {"node": size_node, "property_name": "hauto",
                            "value": target}, show=400)
            error, mtext = await call(session, "comsol_mesh",
                                      {"action": "build_stats"}, show=700)
            if error:
                print("ABORT: mesh build failed.", flush=True)
                return 4
            stats = (parse_result(mtext) or {}).get("statistics") or {}
            if stats.get("NumElem") is not None:
                print(f"  mesh rebuilt: {stats['NumElem']} elements", flush=True)
            error, ftext = await call(session, "comsol_focus_desktop", show=300)
            for line in (ftext or "").splitlines():
                if "stale_windows_hidden" in line or "focused" in line:
                    print("   focus: " + line.strip()[:100], flush=True)

        print("\n---- 5. solve ----", flush=True)
        solve_args = {"study": args.study} if args.study else {}
        error, _ = await call(session, "comsol_solve_start", solve_args,
                              show=400)
        if error:
            print("\nABORT: could not start the solve; not polling.", flush=True)
            return 3
        deadline = time.time() + args.timeout
        last_seen, state = "", "solving"
        while time.time() < deadline:
            error, stext = await call(session, "comsol_solve_status", show=0)
            status = parse_result(stext) or {}
            state = status.get("state", "idle") if not error else "failed"
            progress = status.get("progress") or {}
            summary = " | ".join(
                str(progress.get(key)) for key in ("percent", "stage", "last_line")
                if progress.get(key) is not None
            ) or stext.strip().splitlines()[-1] if stext.strip() else ""
            if summary and summary != last_seen:
                print(f"   [{state}] {summary[:140]}", flush=True)
                last_seen = summary
            if state in ("done", "failed"):
                break
            await asyncio.sleep(POLL_SECONDS)
        if state == "failed":
            print("ABORT: solve failed. The last status line above has the "
                  "COMSOL error.", flush=True)
            return 3
        if state != "done":
            print(f"SOLVE TIMEOUT after {args.timeout}s - showing whatever "
                  "is solved so far; results may be incomplete.", flush=True)

        print("\n---- 6. results: evaluate + plot ----", flush=True)
        expression = args.expression or auto_expression(desc)
        eval_args = {"expression": expression}
        if args.dataset:
            eval_args["dataset"] = args.dataset
        error, etext = await call(session, "comsol_evaluate", eval_args,
                                  show=500)
        if not error:
            evaled = parse_result(etext) or {}
            stats = evaled.get("statistics") or (
                numeric_stats(evaled.get("values"))
                if evaled.get("values") is not None else None)
            if stats:
                print(f"  {expression}: min={stats['min']:.6g} "
                      f"max={stats['max']:.6g} mean={stats['mean']:.6g}",
                      flush=True)
            if evaled.get("file"):
                print(f"  full values -> {evaled['file']}", flush=True)

        image = None
        plot_group, all_groups = pick_plot_group(desc, args.plot_hint)
        print(f"  plot groups: {all_groups}, exporting: {plot_group}", flush=True)
        if plot_group:
            error, itext = await call(session, "comsol_export_plot_image",
                                      {"plot_group": plot_group}, show=400)
            if not error:
                image = (parse_result(itext) or {}).get("file")
            await call(session, "comsol_show_results",
                       {"plot_group": plot_group}, show=300)
            error, ftext2 = await call(session, "comsol_focus_desktop", show=300)
            for line in (ftext2 or "").splitlines():
                if "stale_windows_hidden" in line or "focused" in line:
                    print("   focus: " + line.strip()[:100], flush=True)
        print(f"\nWORKFLOW DONE. image={image}", flush=True)
        code = 0 if state == "done" else 8

        if args.save_as:
            print("\n---- 6.5 save solved model ----", flush=True)
            target = Path(args.save_as)
            target = (target if target.is_absolute()
                      else Path.cwd() / target).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            save_args = {"path": str(target)}
            if target.exists():
                print(f"ABORT: refusing to overwrite {target} "
                      "(pass a new path).", flush=True)
                return 4
            serr, stxt = await call(session, "comsol_save_model",
                                    save_args, show=500)
            if serr:
                print("ABORT: saving the solved model failed.", flush=True)
                return 4
            saved = (parse_result(stxt) or {}).get("saved_to") or str(target)
            print(f"  solved model -> {saved}", flush=True)

        if not args.no_close:
            print("\n---- 7. clean shutdown ----", flush=True)
            await call(session, "comsol_end_session",
                       {"save": False, "stop_server": True, "close_gui": True},
                       show=1200)
            started_session = False
            # The engine's close handshake races COMSOL's own exit animation:
            # it can report still_open for a process that dies a moment later,
            # stranding <model>.mph.lock. Re-check and sweep once the smoke
            # clears - same guard as preflight: only when no COMSOL is live.
            for _ in range(10):
                if not comsol_processes():
                    break
                await asyncio.sleep(1.0)
            handle_lock(model, force=False)
            if comsol_processes():
                print("NOTE: a COMSOL window is still open - close it from "
                      "its UI (never force-kill).", flush=True)
            else:
                print("shutdown verified: no COMSOL processes, no lock.",
                      flush=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        traceback.print_exc()
        code = 7
    finally:
        if started_session and not args.no_close:
            await cleanup(session, "ending session after early exit/error")
    return code


def run_check_only(args) -> int:
    """Offline preflight: model, lock, port, repo, engine contract (no JVM)."""
    print("== comsol-dsh preflight (--check, nothing is started) ==", flush=True)
    ok = True
    model = Path(args.model).resolve()
    print(f"model:        {model} exists={model.is_file()}", flush=True)
    ok &= model.is_file()
    lock = model.with_name(model.name + ".lock")
    print(f"stale lock:   {lock} exists={lock.exists()}", flush=True)
    procs = comsol_processes()
    print(f"comsol procs: {procs or 'none'}", flush=True)
    print(f"port {args.port}:     busy={port_busy(args.port)} "
          "(a free port means a fresh server; busy means --reuse-port)",
          flush=True)
    repo = Path(args.mcp_repo).resolve() if args.mcp_repo else resolve_repo()
    print(f"engine repo:  {repo}", flush=True)
    if repo is None:
        print("FAIL: comsol-mcp checkout not found (set COMSOL_MCP_REPO)",
              flush=True)
        return 4
    server_env = dict(os.environ)
    server_env["PYTHONPATH"] = str(repo / "src")
    repo_src = str(repo / "src")
    if repo_src not in sys.path:
        sys.path.insert(0, repo_src)
    import comsol_mcp  # noqa: F401  (verifies it is importable)
    print(f"comsol_mcp:   {comsol_mcp.__file__}", flush=True)
    from comsol_mcp.server import build_server
    tools = asyncio.run(_list_server_tools(build_server))
    registry = {t["name"]: t["params"] for t in tools}
    problems = []
    for name, required in sorted(REQUIRED_TOOLS.items()):
        if name not in registry:
            problems.append(f"missing tool {name}")
        else:
            missing = [p for p in required if p not in registry[name]]
            if missing:
                problems.append(f"{name} lacks {missing}")
    print(f"engine tools: {len(registry)}", flush=True)
    if problems:
        for line in problems:
            print(f"  contract problem: {line}", flush=True)
        ok = False
    try:
        from comsol_mcp import env as cm_env
        installs = cm_env.find_installations()
        roots = [str(i.root) for i in installs]
        print("COMSOL:       " + (", ".join(roots) if roots
                                 else "NOT FOUND (set COMSOL_ROOT)"), flush=True)
        ok &= bool(installs)
    except Exception as exc:
        print(f"COMSOL detect failed: {exc}", flush=True)
        ok = False
    print(f"\n{'PREFLIGHT OK' if ok else 'PREFLIGHT FAILED'}", flush=True)
    return 0 if ok else 1


async def _list_server_tools(build_server_fn):
    mcp = build_server_fn()
    return [{"name": t.name,
             "params": list((t.inputSchema or {}).get("properties", {}))}
            for t in await mcp.list_tools()]


def main() -> int:
    # COMSOL's progress log carries the model UI language (often Chinese); on
    # an English Windows the default console code page cannot encode it and
    # Python silently replaces those characters with '?'. Always speak UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", required=True, help="absolute path to .mph")
    parser.add_argument("--home", required=True,
                        help="work dir for logs/snapshots/exports")
    parser.add_argument("--port", type=int, default=2036,
                        help="COMSOL server port")
    parser.add_argument("--reuse-port", action="store_true",
                        help="attach to a server already listening on --port")
    parser.add_argument("--timeout", type=int, default=1200,
                        help="solve poll deadline in seconds")
    parser.add_argument("--study", default=None,
                        help="study tag (e.g. std1); omit to solve all")
    parser.add_argument("--dataset", default=None,
                        help="dataset tag for comsol_evaluate")
    parser.add_argument("--set-param", action="append", default=None,
                        metavar="NAME=VALUE",
                        help="global parameter override (repeatable); "
                             "omit to make no parameter changes")
    parser.add_argument("--hauto", default=None,
                        help="mesh 'max element size' quality level 1-5, or "
                             "'none' to keep; default: one step finer")
    parser.add_argument("--no-mesh", action="store_true",
                        help="skip the mesh size change + rebuild")
    parser.add_argument("--no-solver-tweak", action="store_true",
                        help="skip the solver tolerance/plot tweak")
    parser.add_argument("--expression", default=None,
                        help="expression to evaluate after solving "
                             "(default: auto-pick from physics)")
    parser.add_argument("--plot-hint", default="auto",
                        help="'auto', a keyword like 'temperature', "
                             "or a plot group tag")
    parser.add_argument("--no-close", action="store_true",
                        help="leave the session + desktop open at the end")
    parser.add_argument("--save-as", default=None, metavar="PATH",
                        help="after solving, save the solved model to this "
                             "path (never overwrites; the opened file is "
                             "left untouched)")
    parser.add_argument("--allow-code", action="store_true",
                        help="set COMSOL_MCP_ALLOW_CODE=1 for the engine "
                             "(enables comsol_run_python; not needed by "
                             "this workflow)")
    parser.add_argument("--allow-live-lock", action="store_true",
                        help="delete <model>.mph.lock even if COMSOL is "
                             "running (only if you know the lock is orphaned)")
    parser.add_argument("--mcp-repo", default=None,
                        help="comsol-mcp checkout (default: auto-detect)")
    parser.add_argument("--check", action="store_true",
                        help="offline preflight only; starts no COMSOL")
    args = parser.parse_args()
    if args.check:
        return run_check_only(args)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())

