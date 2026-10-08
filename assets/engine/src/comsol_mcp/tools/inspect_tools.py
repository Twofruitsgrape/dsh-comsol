"""Model understanding tools: describe, node tree, properties, source export."""

from __future__ import annotations

import logging
from pathlib import Path

from .. import model_access
from ..errors import ModelError, NodeError
from ..runtime import Runtime, guarded
from ..snapshot import save_copy as model_access_snapshot_save_copy

log = logging.getLogger(__name__)


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_describe_model(sections: list[str] | None = None) -> dict:
        """Structured overview of the main model: parameters, components, geometry,
        materials, physics, mesh, studies, solutions and results.

        Start every "understand this model" request here. Args:
            sections: optional subset of ["model", "parameters", "components",
                "studies", "results"]; default is everything.
        """
        def work():
            session = runtime.session
            model = session.main_model()
            summary = model_access.model_summary(model.java, sections)
            summary["main_model"] = session.main.as_dict() if session.main else None
            return summary

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_node_tree(path: str | None = None, depth: int = 1) -> dict:
        """Browse the model tree below `path` (default: the model root).

        Path syntax: `component/comp1/physics/es`, `study/std1`, `result/pg1`,
        `component/comp1/mesh/mesh1/feature/size`, ... Use this to discover tags
        before comsol_get_property / comsol_set_property / comsol_run_feature.
        """
        model = await runtime.run(runtime.session.main_model)
        bounded = max(1, min(int(depth), 4))
        return await runtime.run(model_access.tree, model.java, path, bounded)

    @mcp.tool()
    @guarded
    async def comsol_get_property(node: str, property: str | None = None,
                                  limit: int = 100) -> dict:
        """Read a model-tree node's properties (or one named property).

        Args:
            node: node path, e.g. "component/comp1/physics/es/feature/f1".
            property: read only this property; omit to list all (up to `limit`).
        """
        model = await runtime.run(runtime.session.main_model)
        bounded = max(1, min(int(limit), 400))

        def work():
            target = model_access.resolve(model.java, node)
            data = model_access.node_properties(target, limit=bounded)
            if property:
                entry = data["properties"].get(property)
                if entry is None:
                    raise NodeError(
                        f"Property '{property}' was not found on '{node}'.",
                        hint="Call comsol_get_property without `property` to see what exists.",
                    )
                return {property: entry}
            return data

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_export_source(path: str, format: str = "java") -> dict:
        """Save the model as source code for deep reading (format: java, matlab, vba).

        The generated source is a complete, greppable description of every model
        setting - the best way to truly understand a model. The file is written
        to `path` and its location returned; read it with the normal file tools.
        """
        def work():
            session = runtime.session
            model = session.main_model()
            target = Path(path).expanduser().resolve()
            if target.exists():
                raise ModelError(
                    f"Refusing to overwrite the existing file {target}.",
                    hint="Pass a new file name (e.g. with a timestamp).",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            model_access_snapshot_save_copy(model, str(target), format)
            if not target.exists():
                candidates = sorted(target.parent.glob(target.stem + ".*"))
                if not candidates:
                    raise ModelError("COMSOL reported success but no file was written.")
                target = candidates[0]
            return {
                "source_file": str(target),
                "size_bytes": target.stat().st_size,
                "note": "Read or grep this file to understand the model in depth; it can be long.",
            }

        return await runtime.run(work)
