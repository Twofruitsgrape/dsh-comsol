"""Unit tests for GPU (hardware acceleration) helpers - no COMSOL needed."""

from __future__ import annotations

import pytest

from comsol_mcp import gpu
from comsol_mcp.errors import ComsolMcpError

TRUTHY = {"on": True, "off": False, "1": True, "0": False}


class Collection:
    def __init__(self, children: dict | None = None):
        self.children = dict(children or {})

    def tags(self) -> list[str]:
        return list(self.children)

    def get(self, tag: str):
        return self.children[tag]


class Accessor:
    """Callable both as collection (no tag) and as child lookup (tag)."""

    def __init__(self, node):
        self.node = node

    def __call__(self, tag: str | None = None):
        if tag is None:
            return self.node.collection()
        return self.node.collection().children[tag]


class Feature:
    def __init__(self, kind: str, props: dict | None = None, allow=("on", "off", "1", "0")):
        self.kind = kind
        self.props = dict(props or {})
        self.allow = allow
        self.children = Collection()
        self.feature = Accessor(self)

    def collection(self) -> Collection:
        return self.children

    def getType(self) -> str:
        return self.kind

    def getString(self, name: str) -> str:
        if name not in self.props:
            raise KeyError(name)
        return self.props[name]

    def set(self, name: str, value: str) -> None:
        if name not in self.props:
            raise KeyError(name)
        if value not in self.allow:
            raise ValueError(f"invalid value {value}")
        self.props[name] = "on" if TRUTHY[value] else "off"

    def hasProperty(self, name: str) -> bool:
        return name in self.props

    def properties(self) -> list[str]:
        return list(self.props)

    def create(self, tag: str, kind: str):
        child = Feature(kind, {"dgacc": "off", "usemaxblocksize": "on",
                               "gpuresidual": "on", "gpusingleprecision": "off"})
        self.children.children[tag] = child
        return child


class PhysicsNode(Feature):
    def __init__(self, kind: str, settings: Feature | None):
        super().__init__(kind)
        self.settings = settings

    def prop(self, name: str):
        if self.settings is None or name != gpu.PHYSICS_SETTINGS_GROUP:
            raise KeyError(name)
        return self.settings


class SolverNode(Feature):
    def __init__(self, kind: str, study: str | None = None, features: dict | None = None):
        super().__init__(kind)
        self.children = Collection(features)
        self.feature = Accessor(self)
        self.study_tag = study

    def study(self) -> str:
        return self.study_tag or ""


class Java:
    """Minimal stand-in for model.java."""

    def __init__(self, solvers: dict | None = None, physics: dict | None = None,
                 studies: dict | None = None):
        self._solvers = Collection(solvers or {})
        self._physics = Collection(physics or {})
        self._studies = Collection(studies or {})

    def _accessor(self, collection: Collection):
        class _A:
            def __call__(_self, tag: str | None = None):
                if tag is None:
                    return collection
                return collection.children[tag]

        return _A()

    def sol(self, tag: str | None = None):
        if tag is None:
            return self._solvers
        return self._solvers.children[tag]

    def physics(self, tag: str | None = None):
        if tag is None:
            return self._physics
        return self._physics.children[tag]

    def study(self, tag: str | None = None):
        if tag is None:
            return self._studies
        return self._studies.children[tag]


def transient_java(physics_settings: Feature | None = None, solver_kind: str = "Time") -> Java:
    time_node = SolverNode(solver_kind, study="std1")
    sol1 = SolverNode("Solution", study="std1", features={"t1": time_node})
    interfaces = {}
    if physics_settings is not None:
        interfaces["acpr"] = PhysicsNode("PressureAcousticsTimeExplicit", physics_settings)
    return Java(solvers={"sol1": sol1}, physics=interfaces, studies={"std1": Feature("Study")})


# --------------------------------------------------------------------- solvers


def test_describe_model_without_acceleration():
    java = transient_java()
    report = gpu.describe_model(java)
    assert report["acceleration_nodes"] == 0
    assert report["solvers"][0]["solver"] == "sol1"
    assert report["solvers"][0]["nodes"][0] == {"tag": "t1", "type": "Time"}
    assert report["physics"] == []
    assert "discontinuous-Galerkin" in report["scope"]


def test_configure_creates_node_and_sets_flags():
    java = transient_java()
    result = gpu.configure(java, enabled=True)
    assert result["warnings"] == []
    target = result["targets"][0]
    assert target["acceleration_node"] == "ha1"
    assert target["created"] is True
    assert target["properties"]["dgacc"] == {"before": False, "after": True}
    assert target["properties"]["gpusingleprecision"] == {"before": False, "after": False}

    reported = gpu.describe_model(java)
    assert reported["acceleration_nodes"] == 1
    node = reported["solvers"][0]["nodes"][0]["acceleration"][0]
    assert node["tag"] == "ha1"
    assert node["properties"]["dgacc"] is True
    assert node["properties"]["usemaxblocksize"] is True


def test_configure_is_idempotent_and_can_disable():
    java = transient_java()
    gpu.configure(java, enabled=True)
    again = gpu.configure(java, enabled=False, single_precision=False)
    target = again["targets"][0]
    assert target["created"] is False
    assert target["acceleration_node"] == "ha1"
    assert target["properties"]["dgacc"]["after"] is False

    report = gpu.describe_model(java)
    assert report["acceleration_nodes"] == 1


def test_configure_honours_precision_and_residual_options():
    java = transient_java()
    result = gpu.configure(java, compute_residual=False, single_precision=True)
    props = result["targets"][0]["properties"]
    assert props["gpuresidual"]["after"] is False
    assert props["gpusingleprecision"]["after"] is True


def test_configure_reports_a_model_without_time_solver():
    stationary = SolverNode("Stationary", study="std1")
    sol1 = SolverNode("Solution", study="std1", features={"s1": stationary})
    java = Java(solvers={"sol1": sol1}, studies={"std1": Feature("Study")})
    result = gpu.configure(java)
    assert result["targets"] == []
    assert any("no Time-Dependent solver" in warning for warning in result["warnings"])


def test_configure_rejects_unknown_solver_node():
    java = transient_java()
    with pytest.raises(ComsolMcpError, match="No Time-Dependent solver node"):
        gpu.configure(java, solver_node="t9")


def test_configure_rejects_unknown_sequence():
    java = transient_java()
    with pytest.raises(ComsolMcpError, match="does not exist"):
        gpu.configure(java, sequence="sol9")


# --------------------------------------------------------------------- physics


def test_describe_physics_lists_flags():
    settings = Feature("Settings", {"AcceleratedFormulation": "off",
                                    "GPUResidual": "on",
                                    "GPUSinglePrecision": "off"})
    java = transient_java(settings)
    report = gpu.describe_model(java)
    entry = report["physics"][0]
    assert entry["tag"] == "acpr"
    assert entry["time_explicit"] is True
    assert entry["gpu"]["AcceleratedFormulation"] is False


def test_configure_physics_sets_only_existing_flags():
    settings = Feature("Settings", {"AcceleratedFormulation": "off", "GPUResidual": "on"},
                       allow=("1", "0"))
    java = transient_java(settings)
    result = gpu.configure_physics(java, enabled=True)
    target = result["targets"][0]
    assert target["properties"]["AcceleratedFormulation"] == {"before": False, "after": True}
    assert "GPUSinglePrecision" not in target["properties"]
    assert result["skipped"] == []


def test_configure_physics_skips_interfaces_without_settings():
    plain = Feature("SomePhysics")
    java = Java(physics={"solid": plain})
    result = gpu.configure_physics(java)
    assert result["targets"] == []
    assert result["skipped"][0]["physics"] == "solid"


def test_configure_physics_rejects_unknown_tag():
    java = transient_java(Feature("Settings", {"AcceleratedFormulation": "off"}))
    with pytest.raises(ComsolMcpError, match="does not exist"):
        gpu.configure_physics(java, physics="nope")


# ----------------------------------------------------------------------- hosts


def test_host_report_flags_cuda_12_and_unsupported_card(monkeypatch, tmp_path):
    cuda13 = tmp_path / "CUDA" / "v13.3"
    (cuda13 / "bin").mkdir(parents=True)
    (cuda13 / "bin" / "cudart64_13.dll").write_bytes(b"")

    monkeypatch.setattr(gpu, "_cuda_roots", lambda: [cuda13])
    monkeypatch.setattr(gpu, "_runtime_dlls", lambda root: ["cudart64_13.dll"])
    monkeypatch.setattr(
        gpu,
        "_nvidia_smi",
        lambda: [{"name": "RTX 5060", "compute_capability": 12.0,
                  "driver": "610.62", "memory": "8151 MiB", "supported": False}],
    )
    monkeypatch.setattr(gpu, "_comsol_cuda_preference", lambda: None)

    report = gpu.host_report()
    assert report["cuda_12_runtime"] is False
    assert report["supported_gpus"] == []
    assert "stays on the CPU" in report["conclusion"]
    assert any("support matrix" in note for note in report["notes"])
    assert any("cuda 12" in note.lower() for note in report["notes"])


def test_host_report_accepts_supported_card_with_cuda_12(monkeypatch, tmp_path):
    cuda12 = tmp_path / "CUDA" / "v12.4"
    (cuda12 / "bin").mkdir(parents=True)
    (cuda12 / "bin" / "cudart64_12.dll").write_bytes(b"")

    monkeypatch.setattr(gpu, "_cuda_roots", lambda: [cuda12])
    monkeypatch.setattr(gpu, "_runtime_dlls", lambda root: ["cudart64_12.dll"])
    monkeypatch.setattr(
        gpu,
        "_nvidia_smi",
        lambda: [{"name": "RTX 4090", "compute_capability": 8.9,
                  "driver": "580.10", "memory": "24564 MiB", "supported": True}],
    )
    monkeypatch.setattr(gpu, "_comsol_cuda_preference", lambda: str(cuda12))

    report = gpu.host_report()
    assert report["cuda_12_runtime"] is True
    assert report["supported_gpus"] == ["RTX 4090"]
    assert "can run" in report["conclusion"]
    assert report["notes"] == []
