"""System Atlas currency guard (docs/atlas/).

The atlas is a hand-curated architecture map committed alongside the code it
describes. A stale map is worse than no map, so its claims are mechanically
pinned:

* every node ``path`` must exist in the tree (the drift guard — deleting or
  moving a module goes red on the commit that did it);
* the atlas ``meta`` block must match the shipped version (``pyproject``) and
  ``SCHEMA_META_VERSION`` — a version or schema bump must re-verify the map
  and update ``docs/atlas/atlas.json`` in the same change;
* edges and flows must reference nodes/edges that exist (no dangling ids);
* the committed viewer must load the canonical JSON, not an embedded copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ATLAS_DIR = ROOT / "docs" / "atlas"
ATLAS_JSON = ATLAS_DIR / "atlas.json"
ATLAS_HTML = ATLAS_DIR / "atlas.html"


@pytest.fixture(scope="module")
def atlas() -> dict:
    assert ATLAS_JSON.is_file(), "docs/atlas/atlas.json missing"
    return json.loads(ATLAS_JSON.read_text(encoding="utf-8"))


def _checkable(path: str) -> bool:
    """Node paths that name a literal repo location are checkable; globs,
    parenthesized annotations, and URLs are descriptive only."""
    return bool(path) and "*" not in path and "(" not in path and not path.startswith("http")


def test_node_paths_exist(atlas: dict) -> None:
    missing = [
        f"{n['id']}: {n['p']}"
        for n in atlas["nodes"]
        if _checkable(n.get("p", "")) and not (ROOT / n["p"]).exists()
    ]
    assert not missing, (
        "atlas nodes name paths that no longer exist — update docs/atlas/"
        "atlas.json (and re-verify the affected cards): " + ", ".join(missing)
    )


def test_meta_matches_shipped_version(atlas: dict) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    assert atlas["meta"]["version"] == version, (
        f"atlas meta.version {atlas['meta']['version']!r} != pyproject "
        f"{version!r} — re-verify the map and update docs/atlas/atlas.json "
        "meta (version cut checklist)"
    )


def test_meta_matches_schema_version(atlas: dict) -> None:
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    assert atlas["meta"]["schema"] == SCHEMA_META_VERSION, (
        f"atlas meta.schema {atlas['meta']['schema']} != SCHEMA_META_VERSION "
        f"{SCHEMA_META_VERSION} — re-verify the storage cards and update "
        "docs/atlas/atlas.json meta (schema bump checklist)"
    )


def test_graph_integrity(atlas: dict) -> None:
    node_ids = {n["id"] for n in atlas["nodes"]}
    dangling = [
        f"{e['f']}>{e['t']}"
        for e in atlas["edges"]
        if e["f"] not in node_ids or e["t"] not in node_ids
    ]
    assert not dangling, f"edges reference unknown nodes: {dangling}"

    edge_keys = {f"{e['f']}>{e['t']}" for e in atlas["edges"]}
    for fid, flow in atlas["flows"].items():
        bad = [k for k in flow["seq"] if k not in edge_keys]
        assert not bad, f"flow {fid!r} references unknown edges: {bad}"

    groups = set(atlas["groups"])
    bad_groups = [n["id"] for n in atlas["nodes"] if n["g"] not in groups]
    assert not bad_groups, f"nodes reference unknown groups: {bad_groups}"


def test_viewer_loads_canonical_json() -> None:
    assert ATLAS_HTML.is_file(), "docs/atlas/atlas.html missing"
    html = ATLAS_HTML.read_text(encoding="utf-8")
    assert "atlas.json" in html, "viewer must fetch the canonical atlas.json"
    assert "BASE_NODES = [" not in html, (
        "viewer must not embed a second copy of the node data — "
        "atlas.json is the single source of truth"
    )
