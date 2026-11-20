"""Hardware acceleration (GPU) support for COMSOL 6.3+.

COMSOL 6.3 runs on a GPU in three situations only: the time-explicit pressure
acoustics interface (dG-FEM), a Hardware Acceleration node under a
Time-Dependent solver for discontinuous Galerkin, and DNN surrogate training.
This module exposes the first two as model-tree switches:

* a 'HardwareAcceleration' solver feature under a Time-Dependent solver node
  with the properties 'dgacc' (accelerated solver for discontinuous Galerkin),
  'gpuresidual', 'gpusingleprecision' and 'usemaxblocksize';
* 'SolverSettings' properties on the 'Pressure Acoustics, Time Explicit'
  interface: 'AcceleratedFormulation', 'GPUResidual', 'GPUSinglePrecision'.

Both were verified against a live COMSOL 6.3 session (feature type
HardwareAcceleration, default dgacc=off, gpuresidual=on,
gpusingleprecision=off, usemaxblocksize=on; the interface property is
AcceleratedFormulation - not AcceleratedSolver).

Nothing here makes an unsupported GPU work: COMSOL 6.3 needs an NVIDIA card
with compute capability 6.0-9.0 and a CUDA 12.4 runtime, so host_report()
states those facts instead of promising a speed-up.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import ComsolMcpError

log = logging.getLogger(__name__)

ACCELERATION_TYPE = "HardwareAcceleration"
TIME_SOLVER_TYPE = "Time"
TIME_EXPLICIT_TYPE = "PressureAcousticsTimeExplicit"
PHYSICS_SETTINGS_GROUP = "SolverSettings"

ACCELERATION_PROPERTIES = (
    "dgacc",
    "usemaxblocksize",
    "gpuresidual",
    "gpusingleprecision",
)
PHYSICS_PROPERTIES = ("AcceleratedFormulation", "GPUResidual", "GPUSinglePrecision")

MIN_COMPUTE_CAPABILITY = 6.0
MAX_COMPUTE_CAPABILITY = 9.0
REQUIRED_CUDA_MAJOR = 12


# --------------------------------------------------------------------- probing


def _tags(collection: Any) -> list[str]:
    try:
        return [str(tag) for tag in collection.tags()]
    except Exception:
        return []


def _type_of(node: Any) -> str:
    for name in ("getType", "type"):
        method = getattr(node, name, None)
        if callable(method):
            try:
                return str(method())
            except Exception:
                continue
    return ""


def _collection(node: Any, accessor: str) -> Any:
    method = getattr(node, accessor, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _child(node: Any, accessor: str, tag: str) -> Any:
    method = getattr(node, accessor, None)
    if not callable(method):
        return None
    try:
        return method(tag)
    except Exception:
        return None


def _as_bool(node: Any, name: str) -> bool | None:
    try:
        raw = str(node.getString(name)).strip().lower()
    except Exception:
        return None
    if raw in ("on", "1", "true"):
        return True
    if raw in ("off", "0", "false"):
        return False
    return None


def _set_bool(node: Any, name: str, value: bool) -> None:
    """Write a COMSOL boolean, accepting either spelling (on/off or 1/0)."""
    errors: list[str] = []
    for true_value, false_value in (("on", "off"), ("1", "0")):
        try:
            node.set(name, true_value if value else false_value)
            return
        except Exception as exc:  # the property may only accept the other spelling
            errors.append(str(exc).splitlines()[0])
    raise ComsolMcpError(
        f"Could not write '{name}' = {value}.",
        hint="; ".join(errors[:2]) or None,
    )


def _read_properties(node: Any, names: tuple[str, ...]) -> dict:
    return {name: _as_bool(node, name) for name in names}


# ------------------------------------------------------------------- solvers


def solver_sequences(java: Any) -> list[str]:
    """Tags of the solver sequences in the model, in COMSOL order."""
    return _tags(_collection(java, "sol"))


def solver_nodes(java: Any, sequence: str) -> list[dict]:
    """Solver nodes of one sequence, as {'tag', 'type'} entries."""
    node = _child(java, "sol", sequence)
    if node is None:
        return []
    collection = _collection(node, "feature")
    nodes = []
    for tag in _tags(collection):
        child = _child(node, "feature", tag)
        nodes.append({"tag": tag, "type": _type_of(child)})
    return nodes


def acceleration_nodes(java: Any, sequence: str, solver_node: str) -> list[dict]:
    """HardwareAcceleration features under one solver node."""
    parent = _child(_child(java, "sol", sequence), "feature", solver_node)
    if parent is None:
        return []
    collection = _collection(parent, "feature")
    found = []
    for tag in _tags(collection):
        child = _child(parent, "feature", tag)
        if _type_of(child) != ACCELERATION_TYPE:
            continue
        found.append(
            {
                "tag": tag,
                "properties": _read_properties(child, ACCELERATION_PROPERTIES),
            }
        )
    return found


def describe_model(java: Any) -> dict:
    """Everything the model currently says about GPU execution."""
    sequences = []
    accelerated_nodes = 0
    for sequence in solver_sequences(java):
        nodes = []
        for node in solver_nodes(java, sequence):
            accelerations = acceleration_nodes(java, sequence, node["tag"])
            accelerated_nodes += len(accelerations)
            if accelerations:
                node["acceleration"] = accelerations
            nodes.append(node)
        sequences.append({"solver": sequence, "nodes": nodes})
    return {
        "solvers": sequences,
        "acceleration_nodes": accelerated_nodes,
        "physics": describe_physics(java),
        "scope": "COMSOL 6.3 accelerates only discontinuous-Galerkin time "
                 "simulations (time-explicit pressure acoustics, or the Hardware "
                 "Acceleration node under a Time-Dependent solver) and DNN "
                 "surrogate training.",
    }


def _require_study(java: Any, study: str) -> None:
    studies = _tags(_collection(java, "study"))
    if study not in studies:
        raise ComsolMcpError(
            f"Study '{study}' does not exist.",
            hint=f"Available studies: {studies or 'none'}.",
        )


def ensure_sequence(java: Any, study: str) -> str:
    """Solver sequence for a study, created from the study when missing."""
    _require_study(java, study)
    for sequence in solver_sequences(java):
        node = _child(java, "sol", sequence)
        try:
            linked = str(node.study())
        except Exception:
            linked = ""
        if linked == study:
            return sequence

    collection = _collection(java, "sol")
    existing = set(solver_sequences(java))
    index = 1
    while f"sol{index}" in existing:
        index += 1
    tag = f"sol{index}"
    try:
        collection.create(tag)
        node = _child(java, "sol", tag)
        node.study(study)
        node.createAutoSequence(study)
    except Exception as exc:
        raise ComsolMcpError(
            f"Could not create a solver sequence for study '{study}': {exc}",
            hint="Run the study once in COMSOL, then retry.",
        ) from exc
    return tag


def configure(
    java: Any,
    *,
    enabled: bool = True,
    compute_residual: bool = True,
    single_precision: bool = False,
    max_block_size: bool = True,
    sequence: str | None = None,
    solver_node: str | None = None,
    study: str | None = None,
    create: bool = True,
) -> dict:
    """Create or update HardwareAcceleration nodes; report every change."""
    sequences = solver_sequences(java)
    if study and not sequences:
        sequences = [ensure_sequence(java, study)]
    if sequence is not None:
        if sequence not in sequences:
            raise ComsolMcpError(
                f"Solver sequence '{sequence}' does not exist.",
                hint=f"Available solver sequences: {sequences or 'none'}.",
            )
        sequences = [sequence]

    targets: list[tuple[str, dict]] = []
    for solver in sequences:
        for node in solver_nodes(java, solver):
            if node["type"] != TIME_SOLVER_TYPE:
                continue
            if solver_node is not None and node["tag"] != solver_node:
                continue
            targets.append((solver, node))
    if solver_node is not None and not targets:
        raise ComsolMcpError(
            f"No Time-Dependent solver node '{solver_node}' found.",
            hint="Call comsol_gpu_status to list solver nodes; only a "
                 "Time-Dependent solver can carry hardware acceleration.",
        )

    wanted = {
        "dgacc": enabled,
        "gpuresidual": compute_residual,
        "gpusingleprecision": single_precision,
        "usemaxblocksize": max_block_size,
    }

    changed: list[dict] = []
    warnings: list[str] = []
    for solver, node in targets:
        parent = _child(_child(java, "sol", solver), "feature", node["tag"])
        accelerations = acceleration_nodes(java, solver, node["tag"])
        if accelerations:
            tag = accelerations[0]["tag"]
            feature = _child(parent, "feature", tag)
            created = False
        elif create:
            used = set(_tags(_collection(parent, "feature")))
            index = 1
            while f"ha{index}" in used:
                index += 1
            tag = f"ha{index}"
            try:
                feature = parent.create(tag, ACCELERATION_TYPE)
            except Exception as exc:
                warnings.append(
                    f"{solver}/{node['tag']}: could not add a "
                    f"{ACCELERATION_TYPE} node ({exc})."
                )
                continue
            created = True
        else:
            warnings.append(
                f"{solver}/{node['tag']}: no {ACCELERATION_TYPE} node present."
            )
            continue

        properties: dict[str, dict] = {}
        for name, value in wanted.items():
            before = _as_bool(feature, name)
            if before is not None and before == value:
                properties[name] = {"before": before, "after": before}
                continue
            try:
                _set_bool(feature, name, value)
            except ComsolMcpError as exc:
                warnings.append(f"{solver}/{node['tag']}/{tag}: {exc.message}")
                continue
            properties[name] = {"before": before, "after": _as_bool(feature, name)}
        changed.append(
            {
                "solver": solver,
                "solver_node": node["tag"],
                "acceleration_node": tag,
                "created": created,
                "properties": properties,
            }
        )

    if not targets:
        warnings.append(
            "The model has no Time-Dependent solver node; hardware acceleration "
            "only exists there (plus the time-explicit acoustics interface)."
        )
    return {"enabled": enabled, "targets": changed, "warnings": warnings}


# ------------------------------------------------------------------- physics


def _physics_settings(interface: Any) -> Any:
    method = getattr(interface, "prop", None)
    if not callable(method):
        return None
    try:
        return method(PHYSICS_SETTINGS_GROUP)
    except Exception:
        return None


def _has_property(group: Any, name: str) -> bool:
    checker = getattr(group, "hasProperty", None)
    if callable(checker):
        try:
            return bool(checker(name))
        except Exception:
            return False
    return _as_bool(group, name) is not None


def describe_physics(java: Any) -> list[dict]:
    """Physics interfaces and the GPU flags they expose."""
    collection = _collection(java, "physics")
    found = []
    for tag in _tags(collection):
        interface = _child(java, "physics", tag)
        kind = _type_of(interface)
        group = _physics_settings(interface)
        entry: dict[str, Any] = {
            "tag": tag,
            "type": kind,
            "time_explicit": kind == TIME_EXPLICIT_TYPE,
        }
        if group is None:
            entry["gpu"] = None
            found.append(entry)
            continue
        flags = {
            name: _as_bool(group, name)
            for name in PHYSICS_PROPERTIES
            if _has_property(group, name)
        }
        entry["gpu"] = flags or None
        found.append(entry)
    return found


def configure_physics(
    java: Any,
    *,
    enabled: bool = True,
    compute_residual: bool = True,
    single_precision: bool = False,
    physics: str | None = None,
) -> dict:
    """Toggle the accelerated formulation of the interfaces that expose it."""
    collection = _collection(java, "physics")
    tags = _tags(collection)
    if physics is not None:
        if physics not in tags:
            raise ComsolMcpError(
                f"Physics interface '{physics}' does not exist.",
                hint=f"Available physics interfaces: {tags or 'none'}.",
            )
        tags = [physics]

    wanted = {
        "AcceleratedFormulation": enabled,
        "GPUResidual": compute_residual,
        "GPUSinglePrecision": single_precision,
    }
    changed: list[dict] = []
    skipped: list[dict] = []
    for tag in tags:
        interface = _child(java, "physics", tag)
        group = _physics_settings(interface)
        if group is None:
            skipped.append({"physics": tag, "reason": "no SolverSettings group"})
            continue
        properties: dict[str, dict] = {}
        for name, value in wanted.items():
            if not _has_property(group, name):
                continue
            before = _as_bool(group, name)
            if before is not None and before == value:
                properties[name] = {"before": before, "after": before}
                continue
            try:
                _set_bool(group, name, value)
            except ComsolMcpError as exc:
                properties[name] = {"error": exc.message}
                continue
            properties[name] = {"before": before, "after": _as_bool(group, name)}
        if properties:
            changed.append(
                {"physics": tag, "type": _type_of(interface), "properties": properties}
            )
        else:
            skipped.append(
                {
                    "physics": tag,
                    "reason": "no accelerated-formulation flag "
                              "(only Pressure Acoustics, Time Explicit has one)",
                }
            )
    return {"enabled": enabled, "targets": changed, "skipped": skipped}


# --------------------------------------------------------------------- hosts


def _cuda_roots() -> list[Path]:
    roots: list[Path] = []
    for name, value in os.environ.items():
        if not value:
            continue
        if name == "CUDA_PATH" or name.startswith("CUDA_PATH_V"):
            path = Path(value)
            if path not in roots:
                roots.append(path)
    base = Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA")
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and child not in roots:
                roots.append(child)
    return roots


def _runtime_dlls(root: Path) -> list[str]:
    found: list[str] = []
    for pattern in ("bin/cudart64_*.dll", "bin/x64/cudart64_*.dll"):
        for match in glob.glob(str(root / pattern)):
            found.append(Path(match).name)
    return sorted(set(found))


def _comsol_cuda_preference() -> str | None:
    home = Path.home() / ".comsol"
    if not home.is_dir():
        return None
    candidates = sorted(
        home.glob("v*/comsol.prefs"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for prefs in candidates:
        try:
            for line in prefs.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("gpu.settings.cudaroot="):
                    value = line.split("=", 1)[1].strip()
                    return value.replace("\\:", ":").replace("\\\\", "\\")
        except Exception:
            continue
    return None


def _nvidia_smi() -> list[dict]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,compute_cap,driver_version,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return []
    cards = []
    for line in (result.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            capability = float(parts[1])
        except ValueError:
            capability = None
        cards.append(
            {
                "name": parts[0],
                "compute_capability": capability,
                "driver": parts[2],
                "memory": parts[3] if len(parts) > 3 else None,
                "supported": (
                    capability is not None
                    and MIN_COMPUTE_CAPABILITY <= capability <= MAX_COMPUTE_CAPABILITY
                ),
            }
        )
    return cards


def host_report() -> dict:
    """Best-effort host check: CUDA runtime, GPUs, COMSOL's configured CUDA root."""
    roots = _cuda_roots()
    toolkits = [{"root": str(root), "cudart": _runtime_dlls(root)} for root in roots]
    has_cuda_12 = any(
        any(
            dll.startswith(f"cudart64_{REQUIRED_CUDA_MAJOR}")
            for dll in toolkit["cudart"]
        )
        for toolkit in toolkits
    )
    cards = _nvidia_smi()
    supported_cards = [card for card in cards if card["supported"]]

    notes: list[str] = []
    if not cards:
        notes.append("No NVIDIA GPU was reported (nvidia-smi missing or no driver).")
    elif not supported_cards:
        notes.append(
            "No GPU is inside COMSOL 6.3's support matrix "
            f"(compute capability {MIN_COMPUTE_CAPABILITY:g}-"
            f"{MAX_COMPUTE_CAPABILITY:g})."
        )
    if not has_cuda_12:
        notes.append(
            f"No cudart64_{REQUIRED_CUDA_MAJOR}*.dll was found; COMSOL 6.3 loads the "
            "CUDA 12 runtime, so a CUDA 13-only toolkit does not qualify."
        )
    return {
        "cuda_roots": toolkits,
        "cuda_12_runtime": has_cuda_12,
        "comsol_cuda_preference": _comsol_cuda_preference(),
        "gpus": cards,
        "supported_gpus": [card["name"] for card in supported_cards],
        "conclusion": (
            "Host can run COMSOL 6.3 GPU acceleration."
            if supported_cards and has_cuda_12
            else "Host is missing a supported GPU and/or the CUDA 12 runtime; "
                 "model-side switches can be set but the solve stays on the CPU."
        ),
        "notes": notes,
    }
