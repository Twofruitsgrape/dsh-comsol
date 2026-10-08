"""Unit tests for the generic model-tree access layer (fake COMSOL objects).

The fakes mimic the *verified* COMSOL 6.2 API surface (docs/findings-m0.md):
accessors take an optional tag (`component.physics("es")`), the collection
objects they return answer `tags()` (and `get(tag)`) but are **not callable**.
"""

from __future__ import annotations

import pytest

from comsol_mcp import model_access
from comsol_mcp.errors import NodeError


class Collection:
    """Mimics a COMSOL node list client: `tags()`, `get(tag)`; not callable."""

    def __init__(self, **items):
        self._items = items

    def tags(self):
        return list(self._items)

    def get(self, tag):
        return self._items[tag]


def accessor(items):
    """Mimics a COMSOL accessor: `accessor()` -> list, `accessor(tag)` -> node."""

    def call(tag=None):
        if tag is None:
            return Collection(**items)
        return items[tag]

    return call


class Node:
    def __init__(self, tag, label="", properties=None, is_plot_group=False):
        self._tag, self._label = tag, label
        self._properties = properties or {}
        self._is_plot_group = is_plot_group

    def add(self, name, items):
        setattr(self, name, accessor(items))
        return self

    def tag(self):
        return self._tag

    def label(self):
        return self._label

    def isPlotGroup(self):
        return self._is_plot_group

    def properties(self):
        return list(self._properties)

    def getString(self, name):
        return str(self._properties[name])


class ResultsCollection:
    """Mimics `model.result()`: tags of plot groups plus typed sub-accessors."""

    def __init__(self, groups, datasets, exports, numerical):
        self._groups = Collection(**groups)
        self._datasets = Collection(**datasets)
        self._exports = Collection(**exports)
        self._numerical = Collection(**numerical)

    def tags(self):
        return self._groups.tags()

    def get(self, tag):
        return self._groups.get(tag)

    def dataset(self, tag=None):
        return self._datasets if tag is None else self._datasets.get(tag)

    def export(self, tag=None):
        return self._exports if tag is None else self._exports.get(tag)

    def numerical(self, tag=None):
        return self._numerical if tag is None else self._numerical.get(tag)


class Param:
    """Mimics `model.param()`: the accessor returns the parameter client."""

    def __init__(self, values):
        self._values = values

    def __call__(self):
        return self

    def varnames(self):
        return list(self._values)

    def get(self, name):
        return self._values[name]


def build_model():
    size = Node("size", "Size", {"hauto": "5", "custom": "off"})
    physics = Node("es", "Electrostatics", {"Discretization": "quadratic"}).add(
        "feature", {"size": size}
    )
    component = (
        Node("comp1", "Component 1")
        .add("geom", {"geom1": Node("geom1", "Geometry 1")})
        .add("material", {"mat1": Node("mat1", "Material 1")})
        .add("physics", {"es": physics})
        .add("mesh", {"mesh1": Node("mesh1", "Mesh 1")})
    )
    study = Node("std1", "Study 1").add("feature", {"stat": Node("stat", "Stationary")})
    results = ResultsCollection(
        groups={
            "pg1": Node("pg1", "3D Plot Group 1", is_plot_group=True),
            "pg2": Node("pg2", "2D Plot Group 2", is_plot_group=True),
        },
        datasets={"dset1": Node("dset1", "Study 1/Solution 1")},
        exports={},
        numerical={"gev1": Node("gev1", "Global Evaluation 1")},
    )

    def result_accessor(tag=None):
        if tag is None:
            return results
        return results.get(tag)

    model = (
        Node("Model2", "capacitor_dc.mph")
        .add("component", {"comp1": component})
        .add("study", {"std1": study})
        .add("sol", {"sol1": Node("sol1", "Solution 1")})
        .add("mesh", {"mesh1": Node("mesh1", "Mesh 1")})
    )
    model.result = result_accessor
    model.param = Param({"d": "1[mm]", "V0": "5[V]"})
    return model


# --------------------------------------------------------------- resolve


def test_resolve_node_and_collection():
    model = build_model()
    assert model_access.resolve(model, "component/comp1/physics/es").tag() == "es"
    assert isinstance(model_access.resolve(model, "component/comp1/physics"), Collection)
    assert model_access.resolve(model, "").tag() == "Model2"
    assert model_access.resolve(model, "component/comp1/mesh/mesh1").tag() == "mesh1"
    assert model_access.resolve(model, "result/pg1").tag() == "pg1"


def test_resolve_accepts_aliases_and_model_level_paths():
    model = build_model()
    assert model_access.resolve(model, "comp/comp1/geometry/geom1").tag() == "geom1"
    assert model_access.resolve(model, "study/std1/feature/stat").tag() == "stat"
    assert model_access.resolve(model, "mesh/mesh1").tag() == "mesh1"


def test_resolve_unknown_segment_gives_help():
    model = build_model()
    with pytest.raises(NodeError) as excinfo:
        model_access.resolve(model, "component/comp1/nonsense")
    assert "comsol_node_tree" in (excinfo.value.hint or "")


def test_resolve_wrong_tag_lists_available_tags():
    model = build_model()
    with pytest.raises(NodeError) as excinfo:
        model_access.resolve(model, "component/comp1/physics/nope")
    assert "es" in (excinfo.value.hint or "")


# ------------------------------------------------------------------ tree


def test_tree_lists_children_with_labels():
    model = build_model()
    tree = model_access.tree(model, "component/comp1", depth=1)
    found = {(child["container"], child["tag"]) for child in tree["children"]}
    assert ("physics", "es") in found
    assert ("mesh", "mesh1") in found
    assert ("material", "mat1") in found
    labels = {child["tag"]: child["label"] for child in tree["children"]}
    assert labels["es"] == "Electrostatics"


def test_tree_depth_two_nests_children():
    model = build_model()
    tree = model_access.tree(model, "component/comp1/physics", depth=2)
    assert tree["children"][0]["tag"] == "es"
    assert [child["tag"] for child in tree["children"][0]["children"]] == ["size"]


def test_tree_truncation_flag():
    model = build_model()
    tree = model_access.tree(model, None, depth=4, max_nodes=1)
    assert tree["truncated"] is True


# --------------------------------------------------------- properties


def test_node_properties_reads_values():
    model = build_model()
    node = model_access.resolve(model, "component/comp1/physics/es/feature/size")
    data = model_access.node_properties(node)
    assert data["properties"]["hauto"]["value"] == "5"
    assert data["total"] == 2


def test_node_properties_on_collection_raises_node_error():
    model = build_model()
    collection = model_access.resolve(model, "component/comp1/physics")
    with pytest.raises(NodeError):
        model_access.node_properties(collection)


# ------------------------------------------------------------ summary


def test_model_summary_structure():
    model = build_model()
    summary = model_access.model_summary(model)
    assert summary["model"]["tag"] == "Model2"
    assert summary["parameters"]["values"] == {"d": "1[mm]", "V0": "5[V]"}
    component = summary["components"][0]
    assert component["tag"] == "comp1"
    assert component["label"] == "Component 1"
    assert component["geometry"][0]["tag"] == "geom1"
    assert component["materials"][0]["tag"] == "mat1"
    assert component["physics"][0]["tag"] == "es"
    assert component["physics"][0]["label"] == "Electrostatics"
    assert component["physics"][0]["features"][0]["tag"] == "size"
    assert component["mesh"][0]["tag"] == "mesh1"
    assert summary["studies"][0]["tag"] == "std1"
    assert summary["studies"][0]["features"][0]["tag"] == "stat"
    assert summary["solutions"][0]["tag"] == "sol1"
    assert [group["tag"] for group in summary["results"]["plot_groups"]] == ["pg1", "pg2"]
    assert summary["results"]["datasets"][0]["tag"] == "dset1"
    assert summary["results"]["numerical"][0]["tag"] == "gev1"


def test_model_summary_sections_filter():
    model = build_model()
    summary = model_access.model_summary(model, sections=["parameters"])
    assert "parameters" in summary
    assert "components" not in summary


def test_plot_group_tags_ignores_other_result_nodes():
    model = build_model()
    assert model_access.plot_group_tags(model) == ["pg1", "pg2"]


def test_mesh_statistics_guarded():
    class Stat:
        @staticmethod
        def getNumElem():
            return 1234

        @staticmethod
        def getMinQuality():
            raise RuntimeError("not meshed")

    class MeshNode:
        @staticmethod
        def stat():
            return Stat()

    assert model_access.mesh_statistics(MeshNode()) == {"NumElem": 1234}

    class BrokenMesh:
        @staticmethod
        def stat():
            raise RuntimeError("no mesh")

    assert model_access.mesh_statistics(BrokenMesh()) == {}
    assert model_access.mesh_statistics(None) == {}
