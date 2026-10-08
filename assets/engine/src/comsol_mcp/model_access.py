"""Generic access to the COMSOL model tree.

Path syntax: slash-separated `container/tag` pairs, e.g.

    component/comp1/physics/es/feature/f1
    component/comp1/geometry/geom1/feature/blk1
    component/comp1/mesh/mesh1
    study/std1/feature/time
    result/pg1
    param

Common aliases are accepted (`geometry`/`geom`, `solution`/`sol`,
`plot`/`plotgroup`, ...). A trailing container name resolves to the collection,
so `.../physics` lists the physics interfaces of a component.

Access pattern (verified against COMSOL 6.2, see docs/findings-m0.md):

* a node comes from its **parent accessor with the tag**: `component.physics("es")`,
  `component.mesh("mesh1")`, `model.result("pg1")`;
* the *collection* object returned by `accessor()` is **not callable**
  (`results("pg1")` raises); it answers `tags()`, and mesh/node lists also
  expose `get(tag)`;
* therefore every child lookup goes through `_child(parent, accessor, tag)`.

Everything here is defensive: the Java API differs slightly between COMSOL
versions, and an unknown path must produce a *helpful* error (what exists here
instead), not a stack trace. The agent navigates by trying paths; good errors
are the navigation aid.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import NodeError

log = logging.getLogger(__name__)

ACCESSOR_ALIASES: dict[str, str] = {
    "component": "component",
    "comp": "component",
    "components": "component",
    "geometry": "geom",
    "geom": "geom",
    "geometries": "geom",
    "material": "material",
    "materials": "material",
    "physics": "physics",
    "mesh": "mesh",
    "meshes": "mesh",
    "study": "study",
    "studies": "study",
    "result": "result",
    "results": "result",
    "dataset": "dataset",
    "datasets": "dataset",
    "plotgroup": "plotgroup",
    "plotgroups": "plotgroup",
    "plot": "plotgroup",
    "plots": "plotgroup",
    "export": "export",
    "exports": "export",
    "numerical": "numerical",
    "variable": "variable",
    "variables": "variable",
    "selection": "selection",
    "selections": "selection",
    "feature": "feature",
    "features": "feature",
    "solution": "sol",
    "solutions": "sol",
    "sol": "sol",
    "function": "func",
    "functions": "func",
    "func": "func",
    "coupling": "cpl",
    "couplings": "cpl",
    "cpl": "cpl",
    "probe": "probe",
    "probes": "probe",
    "batch": "batch",
    "multiphysics": "multiphysics",
}

_CONTAINER_NAMES = sorted(set(ACCESSOR_ALIASES.values()))


# --------------------------------------------------------------------- probing


def _tags_of(obj: Any) -> list[str]:
    try:
        return [str(tag) for tag in obj.tags()]
    except Exception:
        return []


def _is_collection(obj: Any) -> bool:
    return callable(getattr(obj, "tags", None))


def _label_of(item: Any) -> str:
    if item is None:
        return ""
    try:
        return str(item.label())
    except Exception:
        return ""


def _type_of(item: Any) -> str | None:
    if item is None:
        return None
    for name in ("getType", "type", "getTag"):
        method = getattr(item, name, None)
        if callable(method):
            try:
                return str(method())
            except Exception:
                continue
    return None


def _info(container: str, tag: str, item: Any) -> dict:
    return {
        "container": container,
        "tag": tag,
        "label": _label_of(item),
        "type": _type_of(item),
    }


def _child(parent: Any, accessor: str, tag: str) -> Any:
    """One child node, via the parent's accessor (the only way that works)."""
    getter = getattr(parent, accessor, None)
    if not callable(getter):
        return None
    try:
        return getter(tag)
    except Exception:
        pass
    try:
        collection = getter()
    except Exception:
        return None
    try:
        return collection.get(tag)  # mesh/node lists expose get(tag)
    except Exception:
        return None


def _containers_of(obj: Any) -> list[str]:
    found: list[str] = []
    for name in _CONTAINER_NAMES:
        method = getattr(obj, name, None)
        if not callable(method):
            continue
        try:
            method()
            found.append(name)
        except Exception:
            continue
    return found


def _children_with_nodes(obj: Any) -> list[tuple[dict, Any]]:
    """Children of a node or a collection, as (info, node) pairs."""
    result: list[tuple[dict, Any]] = []
    if _is_collection(obj):
        for tag in _tags_of(obj):
            try:
                item = obj.get(tag)
            except Exception:
                item = None
            result.append((_info(type(obj).__name__, tag, item), item))
        return result

    for accessor in _containers_of(obj):
        try:
            collection = getattr(obj, accessor)()
        except Exception:
            continue
        for tag in _tags_of(collection):
            item = _child(obj, accessor, tag)
            result.append((_info(accessor, tag, item), item))
    return result


def list_children(obj: Any) -> list[dict]:
    return [info for info, _ in _children_with_nodes(obj)]


def _available_hint(obj: Any) -> str:
    children = list_children(obj)
    if not children:
        return "No child nodes were found here."
    shown = ", ".join(f"{child['container']}/{child['tag']}" for child in children[:12])
    more = "" if len(children) <= 12 else f" ... ({len(children)} total)"
    return f"Available children here: {shown}{more}"


# ------------------------------------------------------------------- resolving


def resolve(model_java: Any, path: str) -> Any:
    """Resolve a container/tag path against the model. Raises NodeError."""
    tokens = [token for token in str(path).replace("\\", "/").split("/") if token.strip()]
    if not tokens:
        return model_java

    node: Any = model_java
    index = 0
    while index < len(tokens):
        raw = tokens[index].strip()
        accessor = ACCESSOR_ALIASES.get(raw.lower())
        if accessor is None:
            raise NodeError(
                f"Unknown path segment '{raw}' in '{path}'.",
                hint="Paths look like component/<tag>/physics/<tag>/feature/<tag>, "
                     "study/<tag>, result/<tag>. Call comsol_node_tree to browse the tree.",
            )
        method = getattr(node, accessor, None)
        if not callable(method):
            where = "/".join(tokens[:index]) or "(model)"
            raise NodeError(
                f"'{accessor}' does not exist at '{where}'.",
                hint=_available_hint(node),
            )
        if index + 1 < len(tokens):
            tag = tokens[index + 1].strip()
            child = _child(node, accessor, tag)
            if child is None:
                try:
                    collection = method()
                except Exception:
                    collection = None
                available = _tags_of(collection) if collection is not None else []
                where = "/".join(tokens[:index]) or "(model)"
                raise NodeError(
                    f"Cannot open {accessor}('{tag}') at '{where}'.",
                    hint=f"Existing {accessor} tags here: {available or 'none'}.",
                )
            node = child
            index += 2
        else:
            try:
                node = method()
            except Exception as exc:
                where = "/".join(tokens[:index]) or "(model)"
                raise NodeError(
                    f"Cannot list {accessor} at '{where}': {exc}",
                    hint=_available_hint(node),
                ) from exc
            index += 1
    return node


def node_info(node: Any) -> dict:
    info: dict[str, Any] = {"type": type(node).__name__}
    for name in ("tag", "label", "name"):
        method = getattr(node, name, None)
        if callable(method):
            try:
                info[name] = str(method())
            except Exception:
                continue
    return info


def node_properties(node: Any, limit: int = 100) -> dict:
    """Readable property values of a node (best effort)."""
    try:
        names = [str(name) for name in node.properties()]
    except Exception as exc:
        raise NodeError(
            f"This node does not expose a property list: {exc}",
            hint="Only model-tree nodes (not collections) have properties.",
        ) from exc
    properties: dict[str, Any] = {}
    for name in names[:limit]:
        value: Any = None
        for reader in ("getString", "get"):
            method = getattr(node, reader, None)
            if callable(method):
                try:
                    value = method(name)
                    break
                except Exception:
                    continue
        if value is None:
            continue
        entry: dict[str, Any] = {"value": str(value)}
        descriptor = getattr(node, "getDescription", None)
        if callable(descriptor):
            try:
                entry["description"] = str(descriptor(name))
            except Exception:
                pass
        properties[name] = entry
    result = {"properties": properties, "total": len(names)}
    if len(names) > limit:
        result["truncated"] = True
    return result


def tree(model_java: Any, path: str | None = None, depth: int = 1, max_nodes: int = 400) -> dict:
    """Browse the model tree below `path` (`None` = model root)."""
    node = model_java if not path else resolve(model_java, path)
    budget = [max_nodes]

    def walk(obj: Any, level: int) -> list[dict] | None:
        if level >= depth or budget[0] <= 0:
            return None
        children = []
        for info, child in _children_with_nodes(obj):
            if budget[0] <= 0:
                break
            budget[0] -= 1
            entry = dict(info)
            grandchildren = walk(child, level + 1) if child is not None else None
            if grandchildren:
                entry["children"] = grandchildren
            children.append(entry)
        return children

    children = walk(node, 0)
    result: dict[str, Any] = {
        "path": path or "(model)",
        "node": node_info(node),
        "children": children or [],
    }
    if budget[0] <= 0:
        result["truncated"] = True
    return result


# ------------------------------------------------------------------- summaries


def _limit(items: list[Any], max_items: int) -> list[Any]:
    return items[:max_items]


def _parameters(model_java: Any, max_items: int) -> dict:
    try:
        param = model_java.param()
        names = [str(name) for name in param.varnames()]
    except Exception:
        return {}
    values = {}
    for name in _limit(names, max_items):
        try:
            values[name] = str(param.get(name))
        except Exception:
            continue
    result: dict[str, Any] = {"count": len(names), "values": values}
    if len(names) > max_items:
        result["truncated"] = True
    return result


def _features(owner: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(owner.feature())
    except Exception:
        return []
    return [
        _info("feature", tag, _child(owner, "feature", tag))
        for tag in _limit(tags, max_items)
    ]


def _physics(component: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(component.physics())
    except Exception:
        return []
    physics = []
    for tag in _limit(tags, max_items):
        item = _child(component, "physics", tag)
        entry = _info("physics", tag, item)
        entry["features"] = _features(item, max_items)
        physics.append(entry)
    return physics


def _geometry(component: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(component.geom())
    except Exception:
        return []
    geometries = []
    for tag in _limit(tags, max_items):
        item = _child(component, "geom", tag)
        entry = _info("geometry", tag, item)
        entry["features"] = _features(item, max_items)
        geometries.append(entry)
    return geometries


def _materials(component: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(component.material())
    except Exception:
        return []
    return [
        _info("material", tag, _child(component, "material", tag))
        for tag in _limit(tags, max_items)
    ]


def mesh_statistics(mesh_node: Any) -> dict:
    """Element/vertex counts and quality of a mesh node (best effort)."""
    stats: dict[str, Any] = {}
    if mesh_node is None:
        return stats
    try:
        stat = mesh_node.stat()
    except Exception:
        return stats
    for name in ("getNumElem", "getNumVertex", "getNumElement", "getMinQuality",
                 "getMeanQuality", "getMaxGrowthRate", "getMinVolume"):
        method = getattr(stat, name, None)
        if callable(method):
            try:
                stats[name.replace("get", "")] = method()
            except Exception:
                continue
    return stats


def _mesh(component: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(component.mesh())
    except Exception:
        return []
    meshes = []
    for tag in _limit(tags, max_items):
        item = _child(component, "mesh", tag)
        entry = _info("mesh", tag, item)
        entry["features"] = _features(item, max_items)
        entry["statistics"] = mesh_statistics(item)
        meshes.append(entry)
    return meshes


def _components(model_java: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(model_java.component())
    except Exception:
        return []
    components = []
    for tag in _limit(tags, max_items):
        item = _child(model_java, "component", tag)
        components.append(
            {
                "tag": tag,
                "label": _label_of(item),
                "geometry": _geometry(item, max_items),
                "materials": _materials(item, max_items),
                "physics": _physics(item, max_items),
                "mesh": _mesh(item, max_items),
            }
        )
    return components


def _studies(model_java: Any, max_items: int) -> list[dict]:
    try:
        tags = _tags_of(model_java.study())
    except Exception:
        return []
    studies = []
    for tag in _limit(tags, max_items):
        item = _child(model_java, "study", tag)
        entry = _info("study", tag, item)
        entry["features"] = _features(item, max_items)
        studies.append(entry)
    return studies


def solutions(model_java: Any, max_items: int) -> list[dict]:
    """Solution nodes of the model (tags and labels)."""
    try:
        tags = _tags_of(model_java.sol())
    except Exception:
        return []
    return [
        _info("solution", tag, _child(model_java, "sol", tag))
        for tag in _limit(tags, max_items)
    ]


def plot_group_tags(model_java: Any) -> list[str]:
    """Tags of the model's plot groups.

    There is no `result().plotGroup()` (verified on 6.2); plot groups are the
    result nodes whose `isPlotGroup()` is true.
    """
    try:
        tags = _tags_of(model_java.result())
    except Exception:
        return []
    groups: list[str] = []
    for tag in [str(tag) for tag in tags]:
        node = _child(model_java, "result", tag)
        checker = getattr(node, "isPlotGroup", None)
        if callable(checker):
            try:
                if bool(checker()):
                    groups.append(tag)
                    continue
            except Exception:
                pass
        if "plotgroup" in str(_type_of(node) or "").lower():
            groups.append(tag)
    return groups


def _results(model_java: Any, max_items: int) -> dict:
    try:
        result = model_java.result()
    except Exception:
        return {}
    results: dict[str, list[dict]] = {}
    for container, key in (("dataset", "datasets"), ("export", "exports"),
                           ("numerical", "numerical")):
        try:
            tags = _tags_of(getattr(result, container)())
        except Exception:
            continue
        results[key] = [
            _info(container, tag, _child(result, container, tag))
            for tag in _limit(tags, max_items)
        ]
    results["plot_groups"] = [
        _info("result", tag, _child(model_java, "result", tag))
        for tag in _limit(plot_group_tags(model_java), max_items)
    ]
    return results


def model_summary(model_java: Any, sections: list[str] | None = None,
                  max_items: int = 40) -> dict:
    """Structured overview of the main model, for the agent to reason about."""
    wanted = {str(section).lower() for section in sections} if sections else None

    def want(name: str) -> bool:
        return wanted is None or name in wanted

    summary: dict[str, Any] = {
        "model": {
            "tag": str(getattr(model_java, "tag", lambda: "")()),
            "label": _label_of(model_java),
        }
    }
    if want("model"):
        # `file()` returns a FileResourceList object; `getFilePath()` gives the path.
        method = getattr(model_java, "getFilePath", None)
        if callable(method):
            try:
                summary["model"]["file"] = str(method())
            except Exception:
                pass
        products = getattr(model_java, "getUsedProducts", None)
        if callable(products):
            try:
                summary["model"]["used_products"] = [str(p) for p in products()]
            except Exception:
                pass
    if want("parameters"):
        summary["parameters"] = _parameters(model_java, max_items)
    if want("components"):
        summary["components"] = _components(model_java, max_items)
    if want("studies"):
        summary["studies"] = _studies(model_java, max_items)
        summary["solutions"] = solutions(model_java, max_items)
    if want("results"):
        summary["results"] = _results(model_java, max_items)
    return summary
