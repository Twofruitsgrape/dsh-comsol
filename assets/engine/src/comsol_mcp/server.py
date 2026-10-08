"""MCP server entry point: builds the FastMCP app and registers every tool."""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import __version__
from .runtime import Runtime
from .tools import register_all

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Drive COMSOL Multiphysics through a COMSOL Multiphysics server while the "
    "COMSOL desktop window stays open and shows every change live. "
    "Typical flow: comsol_detect -> comsol_start_session(model_path=...) -> "
    "comsol_describe_model -> modify (comsol_set_parameters / comsol_set_property) "
    "-> comsol_mesh -> comsol_solve -> comsol_evaluate / comsol_export_plot_image. "
    "Model-changing tools take an automatic snapshot first, and saves never "
    "overwrite the user's original file unless explicitly allowed."
)


def configure_logging() -> None:
    """Logging goes to stderr; stdout must carry the MCP protocol only."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(os.environ.get("COMSOL_MCP_LOG", "INFO").upper())


def preload() -> None:
    """Import the heavy COMSOL stack before serving any request.

    On Windows the first `import numpy` (a large C extension, pulled in by MPh)
    was observed stalling for minutes when it happened inside the request
    worker thread while an MCP client was waiting. Importing it up front, in
    the main thread, keeps every tool call fast and predictable.
    """
    import time

    for module_name in ("numpy", "jpype", "mph"):
        started = time.monotonic()
        try:
            __import__(module_name)
            log.debug("preload: %s imported in %.2fs", module_name, time.monotonic() - started)
        except Exception as exc:  # pragma: no cover - environment specific
            log.warning("preload: %s failed to import: %s", module_name, exc)

    # Start the JVM here as well: doing it later, inside the request worker
    # thread, was observed to hang on Windows while the client was waiting.
    from .session import prepare_client

    prepare_client()


def build_server() -> FastMCP:
    try:
        mcp = FastMCP("comsol-mcp", instructions=INSTRUCTIONS)
    except TypeError:  # older/newer SDK without `instructions`
        mcp = FastMCP("comsol-mcp")
    runtime = Runtime()
    register_all(mcp, runtime)
    return mcp


def main() -> None:
    configure_logging()
    log.info("comsol-mcp %s starting (stdio)", __version__)
    preload()
    try:
        build_server().run()
    finally:
        # MPh's atexit hook flushes stdout while shutting the JVM down. After
        # an MCP stdio client has torn the pipe down, that flush raises
        # "ValueError: I/O operation on closed file" and prints a traceback
        # into the client's log. Detach the streams to a real sink first.
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass


if __name__ == "__main__":
    main()
