# comsol-dsh-skill

Agent Skill: drive COMSOL Multiphysics end to end - open in the desktop,
read/modify parameters, rebuild mesh, tune solver, solve with live progress,
evaluate and export plots.

Engine: `comsol-mcp` (local checkout; stdio MCP
server, MPh -> COMSOL Multiphysics server + visible desktop client). DSH has
no MCP server slot, so this skill is the portable way to reuse the workflow in
any conversation: load `SKILL.md`, run one command.

## Install

Ships as the **`dsh-comsol` plugin**: toggle "COMSOL" in the DSH left-sidebar
plugin panel to make the skill available (on) or remove it (off) in every
workspace. Manual install on a fresh machine:

```powershell
# 1. copy the bundle (this folder goes under assets/comsol-dsh/)
Copy-Item -Recurse <plugin repo> "$env:USERPROFILE\.dsh\plugin-sources\dsh-comsol"
# 2. add to the desktop profile: package.json dependencies
#    "dsh-comsol": "link:C:/Users/<you>/.dsh/plugin-sources/dsh-comsol"
#    and append "dsh-comsol" to dsh.profile.bundles
# 3. resolve the link, then restart DSH
cd "$env:USERPROFILE\.dsh\profiles\desktop"; pnpm install
```

A plain skill drop also works without the plugin: copy this folder to
`%USERPROFILE%\.dsh\skills\comsol-dsh` (then there is no toggle).

Requires: COMSOL 6.0-6.3 (verified 6.2), Python 3.10+, the `mcp` package, and
a `comsol-mcp` checkout. The runner finds the engine via `COMSOL_MCP_REPO` /
`--mcp-repo`, a sibling `comsol-mcp/` folder, or an installed package - in
that order, always preferring the checkout's `src/`.

## Run the full workflow

```powershell
# offline preflight first (no COMSOL started): model/lock/port/engine contract
python scripts/run_workflow.py --model "<abs>.mph" --home "<dir>" --check
# the real thing
python scripts/run_workflow.py --model "<abs>.mph" --home "<dir>" --set-param I=200[A]
```

Defaults are safe on any model: no parameter changes unless `--set-param`,
expression auto-picked from the physics, plot group auto-picked by label,
mesh one `hauto` step finer (or `--no-mesh` / `--hauto none`). Full options:
`--set-param NAME=VALUE` (repeatable), `--study`, `--dataset`, `--hauto`,
`--no-mesh`, `--no-solver-tweak`, `--expression`, `--plot-hint`, `--port`,
`--reuse-port`, `--timeout`, `--no-close`, `--allow-code`, `--mcp-repo`.

Guards built in: refuses a busy port (`--reuse-port` to attach), only deletes
`<model>.mph.lock` when no COMSOL process is live, verifies every tool the
workflow needs against the engine's live registry before starting, and ends
the session cleanly even on early failure. Run in a **foreground** terminal to
see the COMSOL window; a background task's window is invisible on Windows
(session isolation).

## Transient workflow

Turn a steady model into a time-dependent one in one command (adds an Analytic
ramp `Tbc(t)`, rewires the heat BC, builds a Time Dependent study, solves with
COMSOL's own progress window, loads the result into the desktop plot, exports
the per-step T series, saves a new model):

```powershell
python scripts/run_transient.py --model "<in>.mph" --home "<dir>" `
  --save-as "<out>.mph" --allow-code --tlist "range(0,1,60)"
```

Requires `--allow-code` (structural edits run through comsol_run_python).
Never overwrites the output path and never touches the input file. After the
solve the session **stays open so you can inspect the results in the COMSOL
window** - close the window and it cleans itself up (`--hold-max` caps the
wait, `--close` skips the hold, `--shutdown-only --port N` recovers a session
left behind by an interrupted run).

## Tests

```bash
python -m pytest            # offline: contract + unit tests, no COMSOL needed
python -m ruff check .      # lint (same rule set as the engine)
```

## Plugin packaging (self-contained, upload-ready)

The plugin bundle vendors everything it needs - no external checkout or pip
install of the engine is required on the target machine:

```
dsh-comsol/
  index.js                 skill provider (name/description parsed from the
                           SKILL.md frontmatter - single source of truth)
  cordis.patch.yml         mounts the plugin; the panel toggle writes here
  package.json             dsh.bundle manifest (files: index.js + assets)
  assets/comsol-dsh/       this skill (SKILL.md, scripts/, tests/)
  assets/engine/           the comsol-mcp checkout (src/ + tests/, ~150 KB)
```

`run_workflow.py` / `run_transient.py` resolve the engine in this order:
`COMSOL_MCP_REPO` env var -> the vendored `assets/engine` -> a sibling
`comsol-mcp` checkout (development) -> an installed package. Preflight with
`--check` to see which one won.

After editing the skill or the engine, refresh and verify the bundle with:

```powershell
powershell -File comsol-dsh-skill\sync_bundle.ps1
```

It copies both trees into the bundle, then proves the bundle is
self-contained: preflight must resolve the **vendored** engine, and the
plugin must load and serve the skill body. Install manually by copying the
bundle to `%USERPROFILE%\.dsh\plugin-sources\dsh-comsol`, adding it to the
profile's `package.json` (dependency `link:` + bundle entry), running
`pnpm install` there, and restarting DSH.

## Verified

COMSOL 6.2 + MPh 1.4 + Python 3.12, `busbar_terminal.mph` (case library):
- steady: preflight OK -> open 31 s -> `I=200[A]` -> solver `stol=1.0E-4` ->
  `hauto` 5 -> 4, rebuild 4912 elems -> solve `state: done` (Chinese progress
  renders correctly) -> `T`: min 337.683 / max 349.76 / mean 338.831 K ->
  `pg3` plot exported -> clean shutdown: IME ghost swept, save / "server busy"
  dialogs answered, close-report race covered by a post-shutdown lock sweep -
  zero leftover lock, process, or port; no atexit noise.
- transient: `hf1.Text -> Tbc(t)=293.15+60(1-e^(-t/15))` -> std2 0-60 s ->
  COMSOL desktop progress window -> result loaded into `pg3` -> 1392 nodes x
  61 steps, maxT 293.15 -> 307.90 K (+14.75 K) -> saved -> clean shutdown.
- offline: 35 skill tests + 58 engine tests green; bundle resolves its own
  vendored engine and loads as a plugin.
Artifacts: `<home>/exports/` (PNG + CSV), `<home>/history/` (snapshots),
`<home>/logs/` (progress + engine log).

## Rules that matter

Never force-kill the COMSOL window; `Server is in use by another client`
means a modal dialog is open; the desktop only shows models it opened;
`Untitled.mph` means a stale `.mph.lock`; never overwrite saves without
confirmation; getter/setter parameter-name trap (`property` vs
`property_name`). Full playbook: `SKILL.md`.

## License

MIT - see [LICENSE](LICENSE). COMSOL is a trademark of COMSOL AB; this
project is not affiliated with or endorsed by COMSOL AB.




