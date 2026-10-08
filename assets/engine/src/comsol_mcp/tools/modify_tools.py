"""Modification tools: parameters, generic property writes, feature runs, mesh."""

from __future__ import annotations

import logging
import time

from mcp.server.fastmcp import Context

from .. import model_access
from ..errors import ComsolMcpError, ModelError, NodeError
from ..runtime import Runtime, guarded

log = logging.getLogger(__name__)


def _first_tag(collection) -> str | None:
    try:
        tags = [str(tag) for tag in collection.tags()]
    except Exception:
        return None
    return tags[0] if tags else None


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_get_parameters() -> dict:
        """Return all global model parameters (name -> expression, as entered)."""
        def work():
            model = runtime.session.main_model()
            param = model.java.param()
            names = [str(name) for name in param.varnames()]
            return {
                "count": len(names),
                "parameters": {name: str(param.get(name)) for name in names},
            }

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_set_parameters(parameters: dict) -> dict:
        """Set global model parameters (batch). An automatic snapshot is taken first.

        Values are strings exactly as COMSOL expects them, units included,
        e.g. {"R": "5[cm]", "f0": "2.4[GHz]"}; plain numbers are converted.
        """
        def work():
            if not parameters:
                raise ComsolMcpError(
                    "No parameters were given.",
                    hint='Pass at least one, e.g. {"d": "2[mm]"}.',
                )
            session = runtime.session
            model = session.main_model()
            session.autosnapshot("before-parameters")
            param = model.java.param()
            changed: dict[str, dict] = {}
            for name, value in parameters.items():
                before = None
                try:
                    before = str(param.get(str(name)))
                except Exception:
                    pass
                param.set(str(name), str(value))
                changed[str(name)] = {"before": before, "after": str(value)}
            return {"changed": changed}

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_set_property(node: str, property_name: str, value: str) -> dict:
        """Set one property of a model-tree node. An automatic snapshot is taken first.

        Read the current value with comsol_get_property first. COMSOL accepts
        strings such as "1[mm]", "on"/"off", "5", "1e-3".
        """
        def work():
            session = runtime.session
            model = session.main_model()
            target = model_access.resolve(model.java, node)
            before = None
            try:
                before = str(target.getString(property_name))
            except Exception:
                pass
            session.autosnapshot(f"before-set-{property_name}")
            try:
                target.set(property_name, str(value))
            except Exception as exc:
                raise NodeError(
                    f"Could not set '{property_name}' on '{node}': {exc}",
                    hint="Check the property name and the value format with comsol_get_property.",
                ) from exc
            after = None
            try:
                after = str(target.getString(property_name))
            except Exception:
                pass
            return {"node": node, "property": property_name, "before": before, "after": after}

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_run_feature(node: str, ctx: Context = None) -> dict:
        """Run one model-tree node: a geometry feature, mesh, study, solver, plot group.

        Long operations stream COMSOL progress into the conversation.
        """
        session = runtime.session
        session.require_connected()
        started = time.time()

        def work():
            model = session.main_model()
            target = model_access.resolve(model.java, node)
            runner = getattr(target, "run", None)
            ran = node
            if not callable(runner):
                # Some nodes have no run() of their own (e.g. a geometry's
                # "Form Union"): walk up until a runnable ancestor is found -
                # for a finalize feature that is the geometry sequence itself.
                parts = node.split("/")
                for up in range(1, 4):
                    ancestor_path = "/".join(parts[:-up])
                    if not ancestor_path:
                        break
                    ancestor = model_access.resolve(model.java, ancestor_path)
                    runner = getattr(ancestor, "run", None)
                    if callable(runner):
                        ran = ancestor_path
                        break
                else:
                    raise NodeError(
                        f"'{node}' cannot be run.",
                        hint="Only features, mesh nodes, studies and plot groups have run().",
                    )
            session.autosnapshot("before-run")
            if session.progress is not None:
                session.progress.reset()
            runner()
            return {"ran": ran, "seconds": round(time.time() - started, 1)}

        return await runtime.run_streaming(ctx, work, poll=1.5)

    @mcp.tool()
    @guarded
    async def comsol_mesh(action: str = "build_stats", component: str | None = None,
                          mesh: str | None = None, ctx: Context = None) -> dict:
        """Build the mesh and/or report mesh statistics.

        Args:
            action: "build", "stats", or "build_stats" (default).
            component: component tag; default: the first component.
            mesh: mesh tag; default: the first mesh in that component.
        """
        session = runtime.session
        session.require_connected()

        def resolve_mesh(model):
            # COMSOL collections are not callable: use the parent accessor with
            # the tag (component.mesh("mesh1")), never collection("mesh1").
            java = model.java
            comp_tags = [str(tag) for tag in java.component().tags()]
            comp_tag = component or (comp_tags[0] if comp_tags else None)
            if comp_tag is None:
                raise ModelError(
                    "The model has no components.",
                    hint="Create a component first (in the COMSOL desktop).",
                )
            mesh_tags = [str(tag) for tag in java.component(comp_tag).mesh().tags()]
            mesh_tag = mesh or (mesh_tags[0] if mesh_tags else None)
            if mesh_tag is None:
                raise ModelError(
                    f"Component '{comp_tag}' has no mesh node.",
                    hint="Create a mesh node in COMSOL, then retry.",
                )
            node = model_access.resolve(java, f"component/{comp_tag}/mesh/{mesh_tag}")
            return comp_tag, mesh_tag, node

        def stats_of(mesh_node) -> dict:
            return model_access.mesh_statistics(mesh_node)

        def work():
            model = session.main_model()
            comp_tag, mesh_tag, mesh_node = resolve_mesh(model)
            result: dict = {"component": comp_tag, "mesh": mesh_tag}
            if action in ("build", "build_stats"):
                session.autosnapshot("before-mesh")
                if session.progress is not None:
                    session.progress.reset()
                started = time.time()
                mesh_node.run()
                result["build_seconds"] = round(time.time() - started, 1)
            if action in ("stats", "build_stats"):
                result["statistics"] = stats_of(mesh_node)
            if action not in ("build", "stats", "build_stats"):
                raise ComsolMcpError(
                    f"Unknown action '{action}'.",
                    hint='Use "build", "stats" or "build_stats".',
                )
            return result

        if action in ("build", "build_stats"):
            return await runtime.run_streaming(ctx, work, poll=1.5)
        return await runtime.run(work)
