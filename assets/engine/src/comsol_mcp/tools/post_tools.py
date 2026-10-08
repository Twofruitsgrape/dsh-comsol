"""Post-processing tools: evaluate expressions, export data/images, show plots."""

from __future__ import annotations

import contextlib
import csv
import io
import logging
import os
import time
from pathlib import Path

from .. import model_access
from ..errors import ComsolMcpError, ModelError
from ..runtime import Runtime, guarded

log = logging.getLogger(__name__)

MAX_INLINE_NUMBERS = 2000


def _flatten(data, out: list) -> None:
    if isinstance(data, (list, tuple)):
        for item in data:
            _flatten(item, out)
        return
    out.append(data)


def _as_number(value):
    if isinstance(value, complex):
        return f"{value.real:g}{value.imag:+g}i"
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _package(expression: str, data, session) -> dict:
    if hasattr(data, "tolist"):
        data = data.tolist()
    flat: list = []
    _flatten(data, flat)
    numbers = [_as_number(value) for value in flat]
    result: dict = {"expression": expression, "count": len(numbers)}
    if len(numbers) <= MAX_INLINE_NUMBERS:
        result["values"] = data
        return result
    folder = session.home / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "value"])
        for index, value in enumerate(numbers):
            writer.writerow([index, value])
    numeric = [value for value in numbers if isinstance(value, float)]
    result.update(
        {
            "file": str(target),
            "preview": numbers[:20],
            "note": "Result was too large to inline; read the CSV file.",
        }
    )
    if numeric:
        result["statistics"] = {
            "min": min(numeric),
            "max": max(numeric),
            "mean": sum(numeric) / len(numeric),
        }
    return result


def _filename_of(node) -> str | None:
    try:
        return str(node.getString("filename"))
    except Exception:
        return None


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_evaluate(expression: str, unit: str | None = None,
                              dataset: str | None = None) -> dict:
        """Evaluate a COMSOL expression and return the numbers.

        Examples: "es.normE", "intop1(T)", "V" (with a solution dataset).
        Large results are written to a CSV under `.comsol-mcp/exports/`.

        Args:
            expression: COMSOL expression to evaluate.
            unit: optional target unit, e.g. "V" or "J".
            dataset: dataset tag (solution datasets only).
        """
        def work():
            session = runtime.session
            model = session.main_model()
            try:
                data = model.evaluate(expression, unit=unit, dataset=dataset)
            except Exception as exc:
                raise ComsolMcpError(
                    f"Evaluation of '{expression}' failed: {exc}",
                    hint="Check the expression syntax and the dataset tag "
                         "(comsol_describe_model lists datasets).",
                ) from exc
            return _package(expression, data, session)

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_export_output(node: str | None = None, file: str | None = None) -> dict:
        """Run export nodes of the model (images, data files) and report the files.

        Args:
            node: export node tag (see comsol_describe_model -> results.exports).
                Omit to run every export node in the model.
            file: override the output file name of the given node.
        """
        def work():
            session = runtime.session
            model = session.main_model()

            def export_node(tag: str):
                """Export node from the list client: get(tag), then iteration."""
                exports = model.java.result().export()
                getter = getattr(exports, "get", None)
                if callable(getter):
                    try:
                        return getter(tag)
                    except Exception:
                        pass
                try:
                    for item in exports:
                        if str(item.tag()) == tag:
                            return item
                except Exception:
                    pass
                return None

            exports = model.java.result().export()
            tags = [str(tag) for tag in exports.tags()]
            if node:
                if node not in tags:
                    raise ModelError(
                        f"Export node '{node}' does not exist.",
                        hint=f"Existing export nodes: {tags or 'none'}.",
                    )
                target = export_node(node)
                if target is None:
                    raise ModelError(
                        f"Export node '{node}' could not be resolved.",
                        hint="Recreate the export node and retry.",
                    )
                if file:
                    target.set("filename", str(Path(file).expanduser().resolve()))
                target.run()
                return {"ran": [node], "files": [_filename_of(target)]}
            if not tags:
                raise ModelError(
                    "This model has no export nodes.",
                    hint="Use comsol_export_plot_image to export a plot group, or create "
                         "an export node in the COMSOL desktop.",
                )
            files = []
            for tag in tags:
                node_i = export_node(tag)
                if node_i is not None:
                    node_i.run()
                files.append(_filename_of(node_i))
            return {"ran": tags, "files": files}

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_export_plot_image(plot_group: str | None = None,
                                       file: str | None = None) -> dict:
        """Render a plot group to a PNG image on disk (the agent can then view it).

        Args:
            plot_group: plot group tag such as "pg1"; default: the first plot group.
            file: target path; default: `.comsol-mcp/exports/<tag>-<time>.png`.
        """
        def work():
            session = runtime.session
            model = session.main_model()
            tags = model_access.plot_group_tags(model.java)
            if plot_group:
                if plot_group not in tags:
                    raise ModelError(
                        f"Plot group '{plot_group}' was not found.",
                        hint=f"Plot groups in this model: {tags or 'none'}.",
                    )
                tag = plot_group
            else:
                if not tags:
                    raise ModelError("This model has no plot groups.")
                tag = tags[0]
            if file:
                target = Path(file).expanduser().resolve()
            else:
                folder = session.home / "exports"
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            if target.exists():
                raise ModelError(
                    f"Refusing to overwrite {target}.",
                    hint="Pass a different file name.",
                )
            exports = model.java.result().export()
            temp_tag = "img_ai_export"
            if temp_tag in [str(t) for t in exports.tags()]:
                exports.remove(temp_tag)
            node = exports.create(temp_tag, "Image")
            node.set("sourceobject", tag)
            node.set("filename", str(target))
            try:
                node.run()
            finally:
                try:
                    exports.remove(temp_tag)
                except Exception:
                    pass
            if not target.exists():
                raise ComsolMcpError(
                    "The image export reported success but produced no file.",
                    hint="Known issue on some COMSOL 6.2 setups; try comsol_export_output or "
                         "export the data and plot it externally.",
                )
            return {
                "plot_group": tag,
                "file": str(target),
                "size_bytes": target.stat().st_size,
                "note": "Read this PNG file to look at the result.",
            }

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_show_results(plot_group: str | None = None) -> dict:
        """Update plot groups so the COMSOL desktop shows the latest results.

        Args:
            plot_group: plot group tag; omit to update every plot group.
        """
        def work():
            model = runtime.session.main_model()
            if plot_group:
                groups = [plot_group]
            else:
                groups = model_access.plot_group_tags(model.java)
                if not groups:
                    raise ModelError("This model has no plot groups.")
            # COMSOL collections are not callable: go through the parent
            # accessor with the tag (model.result("pg1")), never result()(tag).
            for tag in groups:
                model.java.result(tag).run()
            return {
                "updated": groups,
                "note": "The COMSOL desktop window shows these plots.",
            }

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_run_python(code: str) -> dict:
        """Advanced escape hatch: run Python with `model`, `java`, `client`, `session` in scope.

        Disabled unless the environment variable COMSOL_MCP_ALLOW_CODE=1 is set
        for the MCP server process. Use it for operations the other tools do not
        cover (creating physics interfaces, selections, complex post-processing).
        """
        if os.environ.get("COMSOL_MCP_ALLOW_CODE", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            raise ComsolMcpError(
                "Arbitrary code execution is disabled.",
                hint="Set COMSOL_MCP_ALLOW_CODE=1 in the MCP server environment and restart "
                     "the MCP server to enable comsol_run_python.",
            )

        def work():
            import mph

            session = runtime.session
            model = session.main_model()
            namespace = {
                "model": model,
                "java": model.java,
                "client": session.client,
                "session": session,
                "mph": mph,
            }
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                exec(compile(code, "<comsol_run_python>", "exec"), namespace)
            return {"ok": True, "output": buffer.getvalue()[-4000:]}

        return await runtime.run(work)
