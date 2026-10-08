---
name: comsol-dsh
description: Drive COMSOL Multiphysics models end to end - open in the desktop, read/modify parameters, rebuild mesh, tune solver, run steady or time-dependent studies with live progress, load results into the desktop plot, evaluate and export. Use when the user asks to run, modify, solve, or inspect a COMSOL (.mph) model.
---

# COMSOL Multiphysics workflow skill

## When to use this skill

The user wants anything done with a COMSOL model: open it, explain it, change a
parameter, refine the mesh, re-solve, get numbers out, or export a plot image.
The engine behind this skill is `comsol-mcp` (a stdio MCP server driving a
COMSOL Multiphysics server + visible desktop client through MPh).

## Install into DSH

This skill ships as the **`dsh-comsol` plugin**. In the DSH left-sidebar plugin
panel, find "COMSOL" and toggle it on; new sessions then offer the `comsol-dsh`
skill automatically (toggling it off removes the skill everywhere). To install
the plugin on a fresh machine, copy the bundle folder into
`%USERPROFILE%/.dsh/plugin-sources/dsh-comsol`, add it to the profile's
`package.json` (`dependencies: {"dsh-comsol": "link:..."}` and append
`"dsh-comsol"` to `dsh.profile.bundles`), run `pnpm install` in the profile
directory, and restart DSH.

The skill body is `SKILL.md` here; the runner is `scripts/run_workflow.py`
(shipped alongside under the plugin's `assets/`). DSH has no per-conversation
MCP slot by default, so COMSOL is driven through that runner - a single
command, no resident server. (DSH does have an optional `dsh-mcp-client`
bridge that would expose `mcp__comsol__*` tools directly; it is deliberately
not mounted because `comsol-mcp` starts a JVM at import, which would then sit
resident for every DSH session.)

## Prerequisites (verify once per machine)

- COMSOL Multiphysics **6.0 - 6.3** (fully verified on **6.2**), local install +
  valid license. MPh auto-discovers it; set `COMSOL_ROOT` (the `.../Multiphysics`
  folder) only if `comsol-mcp detect` finds nothing.
- Python **3.10+** with the engine importable. The runner auto-detects, in order:
  `COMSOL_MCP_REPO` env var / `--mcp-repo`, a sibling `comsol-mcp/` checkout next
  to this skill folder, then an installed `comsol-mcp` package. It always puts
  that checkout's `src/` first on the server's `PYTHONPATH`. Plus the `mcp`
  package (`pip install mcp`) for this script itself.
- **Foreground session to SEE the window.** A COMSOL desktop started from a
  background task lives in another Windows session and is invisible to
  the user. If the user wants to watch, run in their own terminal.

## The one-command workflow (recommended)

```powershell
python <skill>/scripts/run_workflow.py --model "<abs path>.mph" --home "<work dir>"
```

Preflight first, without touching COMSOL:

```powershell
python <skill>/scripts/run_workflow.py --model "<abs>" --home "<dir>" --check
```

Chain: open -> describe -> parameters -> solver tweak -> mesh `hauto` one step
finer + rebuild -> background solve with progress -> evaluate -> export plot
image -> show in desktop -> clean shutdown. Sensible defaults are safe on any
model: **no parameter changes unless `--set-param NAME=VALUE` (repeatable)**,
expression auto-picked from the physics (T / solid.mises / es.normE), plot group
auto-picked by label. Other flags: `--hauto N|none`, `--no-mesh`,
`--no-solver-tweak`, `--study std1`, `--dataset dset1`, `--expression`,
`--plot-hint`, `--timeout`, `--port`, `--reuse-port`, `--no-close`,
`--mcp-repo`.

Lock/port guard: `<model>.mph.lock` is deleted only when no COMSOL process is
live (`--allow-live-lock` to force); the run aborts if the port is already
listening (`--reuse-port` to attach instead). Never run two workflows against
the same port at once. On early failure the session is still ended cleanly
(snapshots cover the work).

## Transient workflow (time-varying boundary condition)

`scripts/run_transient.py` turns a steady model into a Time Dependent one:
adds an Analytic ramp function `Tbc(x)=base+delta*(1-exp(-x/tau))`, rewires a
heat boundary to `Tbc(t)`, creates a transient study, solves, exports the
per-timestep `T` series to CSV, verifies the rise, and saves a new model.

```powershell
python <skill>/scripts/run_transient.py --model "<in>.mph" --home "<dir>" `
  --save-as "<out>.mph" --allow-code `
  --base-k 293.15 --delta-k 60 --tau-s 15 --tlist "range(0,1,60)"
```

It edits model structure (functions/studies/solvers), so it runs through
`comsol_run_python` and **requires `--allow-code`** (hard rule 6). Defaults
auto-detect the boundary (a Temperature `T0`, else a HeatFlux convective
ambient `Text`); override with `--bc-feature`/`--bc-prop`. `--save-as` never
overwrites and never touches the input file.

**Like a manual COMSOL run**: while solving, COMSOL's own progress window is
shown in the desktop (`--stream-progress` keeps the console channel instead);
afterwards the transient dataset is bound to the model's temperature plot
group and rendered, so the window shows the solved result. By default the run
then **holds the session open** for inspection and cleans up automatically the
moment you close the COMSOL window (or after `--hold-max` seconds). `--close`
shuts everything down immediately instead; `--shutdown-only --port N` recovers
a session left behind by an interrupted run.

Field-tested traps on 6.2 the script already handles (do not reintroduce them
when editing by hand):
- **Analytic functions take `x`, not `s`/`t`.** A body using the wrong argument
  name fails deep inside the solve ("Variable: s ... not found"). Build in `x`,
  call as `Func(t)`.
- **Units go inside the expression** (`293.15[K]+60[K]*(...)`); `funcunit` /
  `argunit` are not valid Analytic properties and raise "Unknown".
- **A failed solve strands an empty-shell `solN`** (no features, no datasets);
  stale dataset tags then break `comsol_evaluate` ("Dataset does not exist").
  Drop empty shells before solving and re-derive dataset tags after.
- **In a GUI-owned session you cannot create numerical/plot operations**
  ("Operation cannot be created in this context"). Verify with a Data export
  node (allowed) or a headless reopen (`gui=False`), never `result().numerical()`.
- **`study.createSolver()` / `createAutoSequencesAll()` do not exist on 6.2**;
  the real call is `sol.createAutoSequences(<stepTag>)`, and even that is
  optional because `study.run()` auto-builds the sequence.
- **You cannot leave the desktop open by exiting the runner.** The COMSOL
  desktop client (`comsolmphclient`) shuts itself down when its MCP/engine
  process dies (verified: it survives neither JVM exit nor WMI-detached
  launch). To let the user inspect results, *hold the session* - poll
  `comsol_session_status` until `gui_pid` disappears (user closed the window),
  then end the session cleanly.

## Tool playbook (driving the MCP server directly)

1. `comsol_detect` - installs, desktop processes, port 2036.
2. `comsol_start_session(model_path=...)` - server + desktop open the file;
   the desktop owns the main model (`owner: gui`). Takes ~30-115 s. Normal.
3. `comsol_describe_model` - parameters/components/physics/mesh/studies/
   solutions/results. Start every "understand" request here.
4. `comsol_get_parameters` / `comsol_set_parameters({"I": "200[A]"})` -
   values are strings exactly as COMSOL expects, units included. Snapshots
   automatically first.
5. Solver probe - `comsol_node_tree(path="solution/sol1", depth=4)`, find the
   `Stationary` feature (`s1`); read with `comsol_get_property(node=...)`,
   tweak with `comsol_set_property(node=..., property_name=..., value=...)`.
   **Parameter-name trap:** the getter takes `property`, the setter takes
   `property_name`; mixing them up raises "Unknown argument". Unknown node ->
   skip the tweak, never guess.
6. Mesh - `comsol_get_property(node="component/comp1/mesh/mesh1/feature/size",
   property="hauto")`, set one step finer with
   `comsol_set_property(..., property_name="hauto", value="3")`, then
   `comsol_mesh(action="build_stats")`. If the model has no COMSOL mesh (only
   import/part-work), there is no `size` feature - skip.
7. Solve - `comsol_solve_start({})` (or `{"study": "std1"}`), poll
   `comsol_solve_status` every ~5 s (`state/progress.percent/last_line`, as
   JSON - parse it, do not grep raw text). COMSOL has no cancel API; the Stop
   button in its progress window does that. `state: done` + a solution listed
   before trusting results; `failed` carries the COMSOL error in `error`.
8. Post - `comsol_evaluate(expression="T")` (`values` inline, or `file` +
   `statistics` for large results), `comsol_export_plot_image(plot_group="pg3")`
   (read the PNG back and describe it), `comsol_show_results(plot_group="pg3")`
   so the desktop shows it. Plot group tags/labels come from
   `comsol_describe_model -> results.plot_groups`.
9. `comsol_end_session(save=False, stop_server=True, close_gui=True)` -
   snapshots cover the work; a "save changes?" dialog is answered with No.

## Hard rules (field-tested on 6.2, do not violate)

1. **Never force-kill the COMSOL window** (`taskkill /F`, End task). A killed
   desktop corrupts the server-side session; every later connection fails until
   the server restarts. Ask the user to close it from the COMSOL UI.
2. `Server is in use by another client` almost always means a modal dialog is
   open in the COMSOL window (e.g. "save changes?"). Ask the user to dismiss
   it, then retry. `run_workflow.py` sweeps stale windows via
   `comsol_focus_desktop` after mesh and after results, and once more inside
   the close handshake. The blank box over the model tree is a Chinese-IME
   `CiceroUIWndFrame` ghost: the sweep walks the whole descendant window tree
   and hides only small *floating* residue (never a docked panel, never while
   progress is fresh), best-effort, never force-killed. Dialog dismissal
   matches button labels by normalized prefix, so a Chinese "否(N)" is still
   found by "No"; a lingering "等待服务空闲" frame is answered with
   Quit/Cancel on a second pass.
3. The desktop only shows models **it** opened. To show another model, start a
   new session with that `model_path` - never load via API and claim it shows.
4. `Untitled.mph` instead of the requested model -> stale `<model>.mph.lock`
   (server stopped while the window held the model). The runner only removes it
   when no COMSOL process is running; in future close the window before
   stopping the server (`comsol_end_session(close_gui=True, stop_server=True)`
   does exactly that).
5. `comsol_save_model` **never overwrites** without explicit user confirmation
   (`allow_overwrite=True`). Saves/snapshots go to `<home>/saved|history/`.
6. `comsol_run_python` stays disabled unless the user sets
   `COMSOL_MCP_ALLOW_CODE=1` (the runners expose that as `--allow-code`;
   `run_workflow.py` does not need it, `run_transient.py` requires it and
   refuses to start without it).
7. One desktop per server port. `run_workflow.py` refuses a busy port unless
   `--reuse-port` is passed.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `start_session` times out, desktop reports `['Model1']` | stale `.mph.lock` | runner deletes it when no COMSOL runs; otherwise close the window |
| `Server is in use by another client` | port busy / modal dialog | free the port / dismiss dialog, retry |
| runner aborts "port is already listening" | another session holds 2036 | stop it, use another `--port`, or `--reuse-port` |
| runner aborts "engine does not match" | old comsol-mcp checkout | update the engine or pass `--mcp-repo` |
| `focus_desktop`: `GetClassName not found` | restricted shell blocks win32 `ctypes` | run from a normal terminal; solve is unaffected |
| Window never appears | started from a background task | re-run in the user's foreground terminal |
| `solve_status` stays `idle` | solve never started (fail-fast) | check the `solve_start` error, do not poll forever |
| evaluate/plot shows stale numbers | solve still running when you post-processed | wait for `state: done` in solve_status first |
