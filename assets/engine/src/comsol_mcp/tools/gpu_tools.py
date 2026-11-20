"""GPU acceleration tools: read, enable and disable COMSOL hardware acceleration."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import Context

from .. import gpu
from ..runtime import Runtime, guarded

log = logging.getLogger(__name__)


def register(mcp, runtime: Runtime) -> None:

    @mcp.tool()
    @guarded
    async def comsol_gpu_status() -> dict:
        """Report GPU acceleration: what the model has configured, and whether this host can run it.

        Two halves. 'model' lists every solver sequence and its Time-Dependent
        nodes, the HardwareAcceleration node under each (with dgacc,
        gpuresidual, gpusingleprecision, usemaxblocksize), and the GPU flags the
        physics interfaces expose. 'host' checks the CUDA runtime, the NVIDIA
        cards nvidia-smi reports and COMSOL's configured CUDA root.

        COMSOL 6.3 only accelerates discontinuous-Galerkin time simulations
        (Pressure Acoustics, Time Explicit or a Hardware Acceleration node) and
        DNN surrogate training - it needs compute capability 6.0-9.0 and a CUDA
        12 runtime, so read 'host.conclusion' before promising a speed-up.
        """
        def work():
            model = runtime.session.main_model()
            return {"model": gpu.describe_model(model.java), "host": gpu.host_report()}

        return await runtime.run(work)

    @mcp.tool()
    @guarded
    async def comsol_gpu_configure(
        enabled: bool = True,
        solver: str | None = None,
        solver_node: str | None = None,
        study: str | None = None,
        compute_residual: bool = True,
        single_precision: bool = False,
        max_block_size: bool = True,
        accelerated_formulation: bool = True,
        physics: str | None = None,
    ) -> dict:
        """Turn COMSOL hardware acceleration on or off. An automatic snapshot is taken first.

        On the solver side this creates a HardwareAcceleration node under each
        Time-Dependent solver node (or reuses the existing one) and sets dgacc,
        gpuresidual, gpusingleprecision and usemaxblocksize. On the physics side
        it sets the accelerated formulation of Pressure Acoustics, Time Explicit
        interfaces; interfaces without that flag are reported as skipped.

        Args:
            enabled: true turns acceleration on, false turns it off.
            solver: solver sequence tag such as "sol1"; default: every sequence.
            solver_node: Time-Dependent solver node tag such as "t1"; default: all.
            study: study tag: when the model has no solver sequence yet, one is
                generated from this study so the node can be created.
            compute_residual: compute the residual on the GPU (accuracy vs speed).
            single_precision: run the GPU in single precision (COMSOL default is
                double precision; single is faster and less exact).
            max_block_size: COMSOL's maximum-block-size option (leave on unless
                you know the model needs otherwise).
            accelerated_formulation: also set the time-explicit acoustics flag.
            physics: physics interface tag such as "acpr"; default: all interfaces.
        """
        def work():
            session = runtime.session
            model = session.main_model()
            session.autosnapshot("before-gpu")
            result = gpu.configure(
                model.java,
                enabled=enabled,
                compute_residual=compute_residual,
                single_precision=single_precision,
                max_block_size=max_block_size,
                sequence=solver,
                solver_node=solver_node,
                study=study,
            )
            if accelerated_formulation:
                result["physics"] = gpu.configure_physics(
                    model.java,
                    enabled=enabled,
                    compute_residual=compute_residual,
                    single_precision=single_precision,
                    physics=physics,
                )
            result["note"] = (
                "Settings are model-side only: COMSOL rejects an unsupported GPU "
                "or a missing CUDA 12 runtime at solve time. Check comsol_gpu_status."
            )
            return result

        return await runtime.run(work)
