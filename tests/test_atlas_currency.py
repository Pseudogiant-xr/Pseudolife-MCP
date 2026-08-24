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


def test_migration_list_covers_every_schema_version(atlas: dict) -> None:
    """The schema.py storage card's "Additive migrations: vNN ..." line must
    name every version from where the list starts through the version the
    codebase actually ships (SCHEMA_META_VERSION) — otherwise a schema bump
    that forgets to touch the atlas (issue #184: v30/v31 went missing)
    silently stops being caught."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    node = next(n for n in atlas["nodes"] if n["id"] == "schemaN")
    migrations_line = next(
        (line for line in node["d"] if line.startswith("Additive migrations:")),
        None,
    )
    assert migrations_line is not None, (
        "schemaN card lost its 'Additive migrations: ...' line"
    )
    versions = sorted(int(v) for v in re.findall(r"\bv(\d+)\b", migrations_line))
    assert versions, "no vNN entries found in the migrations line"
    expected = list(range(versions[0], SCHEMA_META_VERSION + 1))
    missing = [v for v in expected if v not in versions]
    assert not missing, (
        f"schemaN migration list is missing v{missing} (SCHEMA_META_VERSION="
        f"{SCHEMA_META_VERSION}) — describe the migration(s) from "
        "pseudolife_memory/storage/schema.py and add them to docs/atlas/"
        "atlas.json"
    )


def test_extractor_size_figure_matches_authoritative_doc(atlas: dict) -> None:
    """README.md is the authoritative site for the extractor-sidecar image
    size (currently ~11.8 GB, measured 2026-08-20 — retired the earlier
    ~9 GB / ~10.4 GB / ~12.6 GB figures). The atlas must quote the same
    figure, not a retired one, wherever it states the sonnet-only
    lighter-by size."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"~([\d.]+ GB) lighter", readme)
    assert match, (
        "README.md no longer states a '~N GB lighter' figure for "
        "sonnet-only — update this test's authoritative source"
    )
    canonical = match.group(1)
    atlas_text = json.dumps(atlas)
    assert f"~{canonical} lighter" in atlas_text, (
        f"docs/atlas/atlas.json does not carry the current ~{canonical} "
        "lighter figure from README.md — it may still publish a retired "
        "extractor image size"
    )


REMOVED_TOOLS = ["memory_trace"]


def test_no_removed_tool_references(atlas: dict) -> None:
    """Tools that have been folded away or dropped from the MCP surface
    must not linger in the atlas (memory_trace was folded into
    memory_search(explain=True) — see CHANGELOG 'Tool-surface gate +
    redundancy trim'). Word-boundary matched so this doesn't false-positive
    on the still-live memory_traces database table."""
    atlas_text = json.dumps(atlas)
    hits = [
        name for name in REMOVED_TOOLS
        if re.search(rf"\b{re.escape(name)}\b", atlas_text)
    ]
    assert not hits, (
        f"docs/atlas/atlas.json still references removed tool(s) {hits} — "
        "point the description at the current tool surface instead"
    )
