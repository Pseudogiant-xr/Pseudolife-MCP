# Atlas Stage 1 — Wiki Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live-rendered wiki page per entity — `GET /api/wiki?entity=X` assembling identity, facts, world facts, relations, mentions, timeline, and review flags in one call, rendered as an article-style panel in the Graph tab.

**Architecture:** One new read-only service method `wiki_page()` composing existing storage/cortex readers (no new tables, no schema bump — only `entities.created_at` gets exposed in two existing SELECTs). One new route. One new frontend module rendering the panel; the Graph view's small node-panel is replaced by it. Galaxy/2D renderers untouched (Stage 2).

**Tech Stack:** Python ASGI (in-repo micro-framework, `web/routes.py` table), psycopg/Postgres, vanilla-JS ES modules (no build step), pytest with PG-backed fixtures.

**Spec:** `docs/superpowers/specs/2026-07-15-atlas-wiki-galaxy-design.md` (§B).

## Global Constraints

- Full suite before final commit: `HF_HUB_OFFLINE=1 python -m pytest tests/` with bench Postgres up at `127.0.0.1:5433` (PG tests skip silently without it — that is NOT a pass).
- Run tests via the project venv: `.venv/Scripts/python.exe -m pytest …`.
- TDD with a watched RED: run each new test and see it fail before implementing.
- CHANGELOG.md entry under `[Unreleased]` (behavior change ⇒ required).
- No `SCHEMA_META_VERSION` bump (no DDL changes in this stage).
- Frontend is vanilla ES modules served from `pseudolife_memory/web/static/` — no npm, no bundler. Match the existing `el()/mount()` DOM style.
- Wiki pages are read-only renders; the UI never creates entities.

---

### Task 1: Expose `entities.created_at` in storage readers

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py:620-647` (`find_entity`) and `:1199-1212` (`load_graph` entity SELECT)
- Test: `tests/test_wiki_page.py` (new)

**Interfaces:**
- Produces: `find_entity(name_norm)` result dict gains `"created_at": float`; `load_graph()["entities"]` rows gain `"created_at": float`. Task 2's `wiki_page()` reads both.

- [ ] **Step 1: Write the failing test**

Create `tests/test_wiki_page.py`. The seed helper is the proven one from
`tests/test_entity_provenance.py` (PG-backed; skips without the bench server):

```python
"""Wiki page assembly — the one-call payload behind GET /api/wiki.
PG-backed (skips without a test server)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path_factory):
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path_factory.mktemp("wiki-svc"), database_url=pg_url)


def _seed(svc, *, band="forever", source="pseudolife"):
    """An entity with a current fact, a traced source entry, a project
    attribution row, and one explicit edge to a second entity."""
    with svc._lock:
        svc._ensure_init()
        eid = svc._resolve_or_create_entity("daemon")["id"]
        other = svc._resolve_or_create_entity("docker-desktop")["id"]
    st = svc._storage
    entry_id = st.insert_entry({
        "band": band, "text": "the daemon runs in docker",
        "embedding": np.zeros(384, dtype=np.float32), "surprise": 0.5, "ts": 1234.0,
        "access_count": 0, "source": source, "superseded_at": None,
        "superseded_by_text": None, "last_logical_turn": None, "episode_id": None,
        "episode_title": None, "tags": [], "slots": [],
    })
    st.conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, "
        "status, confidence, asserted_at, last_confirmed, entity_id) "
        "VALUES ('daemon','role','daemon','role','serves MCP','current',0.9,1.0,1.0,%s)",
        (eid,))
    st.add_trace("daemon", "role", entry_id, 1234.0)
    st.upsert_entity_source(eid, source, "derived", time.time())
    st.conn.execute(
        "INSERT INTO edges (src_id, relation, dst_id, confidence, origin, asserted_at) "
        "VALUES (%s, 'runs-on', %s, 0.9, 'user', 2000.0) ON CONFLICT DO NOTHING",
        (eid, other))
    st.conn.commit()
    return eid, other, entry_id


def test_find_entity_and_load_graph_expose_created_at(svc):
    _seed(svc)
    st = svc._storage
    e = st.find_entity("daemon")
    assert isinstance(e["created_at"], float) and e["created_at"] > 0
    g = st.load_graph()
    assert all(isinstance(row["created_at"], float) for row in g["entities"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wiki_page.py::test_find_entity_and_load_graph_expose_created_at -v`
Expected: FAIL with `KeyError: 'created_at'` (skip = bench PG not up; start it first — a skip is not a RED).

- [ ] **Step 3: Implement — add the column to both readers**

In `find_entity` (postgres.py:620): change `cols` to
`("id", "canonical", "display", "etype", "created_at")` and both SELECTs to
`SELECT id, canonical, display, etype, created_at FROM entities …` /
`SELECT e.id, e.canonical, e.display, e.etype, e.created_at FROM entity_aliases a JOIN …`.

In `load_graph` (postgres.py:1199): change `ent_cols` to
`("id", "canonical", "display", "etype", "created_at")` and its SELECT to
`SELECT id, canonical, display, etype, created_at FROM entities …`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wiki_page.py -v`
Expected: PASS.

- [ ] **Step 5: Guard against regressions elsewhere**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph.py tests/test_graph_store.py tests/test_entity_provenance.py tests/test_graph_review.py -v`
Expected: all PASS (the added key is additive; nothing zips fixed-width rows elsewhere — these suites prove it).

- [ ] **Step 6: Commit**

```bash
git add tests/test_wiki_page.py pseudolife_memory/storage/postgres.py
git commit -m "feat(storage): expose entities.created_at in find_entity/load_graph"
```

---

### Task 2: `MemoryService.wiki_page()` — one-call page assembly

**Files:**
- Modify: `pseudolife_memory/service.py` (add method directly after `entity_provenance`, ~line 3640)
- Test: `tests/test_wiki_page.py`

**Interfaces:**
- Consumes: `find_entity` / `load_graph` `created_at` (Task 1); existing `sources_for_entity(eid)`, `entries_for_entity(eid, limit=)`, `load_communities()["assignment"]`, `pending_entity_proposals()`, `pending_proposals()`, `self._cortex.current_records()`, `self._world.current_records()`, `self.graph_neighborhood(entity, depth=1, include_facts=False)`.
- Produces: `wiki_page(entity: str, *, mentions_limit: int = 20, timeline_limit: int = 30) -> dict` with keys `found, entity, canonical, etype, aliases, projects, community, first_seen, facts, world_facts, relations{out,in}, mentions, timeline, flags` — the exact payload Task 3's route and Task 4's frontend consume.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_wiki_page.py`)

```python
def test_wiki_page_assembles_identity_facts_relations_mentions(svc):
    _seed(svc)
    out = svc.wiki_page("daemon")
    assert out["found"] is True and out["entity"] == "daemon"
    assert out["canonical"] == "daemon" and isinstance(out["first_seen"], float)
    assert any(p["source"] == "pseudolife" for p in out["projects"])
    assert [f["attribute"] for f in out["facts"]] == ["role"]
    assert out["facts"][0]["history_available"] is False
    assert any(r["target"] == "docker-desktop" and r["relation"] == "runs-on"
               for r in out["relations"]["out"])
    assert out["relations"]["in"] == []
    assert out["mentions"] and "docker" in out["mentions"][0]["text"]


def test_wiki_page_timeline_merges_and_orders_newest_first(svc):
    _seed(svc)
    tl = svc.wiki_page("daemon")["timeline"]
    kinds = {t["kind"] for t in tl}
    assert {"entity-created", "edge-asserted", "fact-stamped", "mention"} <= kinds
    ts = [t["ts"] for t in tl]
    assert ts == sorted(ts, reverse=True)


def test_wiki_page_world_facts_filtered_to_entity(svc):
    _seed(svc)
    svc.world_write("daemon", "latest-release", "v2.0",
                    source_url="https://example.com/rel", source_quote="v2.0 shipped")
    svc.world_write("unrelated", "x", "y",
                    source_url="https://example.com/x", source_quote="q")
    wf = svc.wiki_page("daemon")["world_facts"]
    assert [w["attribute"] for w in wf] == ["latest-release"]
    assert wf[0]["source_url"] == "https://example.com/rel"


def test_wiki_page_unknown_entity_not_found(svc):
    assert svc.wiki_page("nonexistent thing")["found"] is False


def test_wiki_page_resolves_colloquial_name_and_flags_unattributed(svc):
    with svc._lock:
        svc._ensure_init()
        svc._resolve_or_create_entity("lonely node")
    svc._storage.conn.commit()
    out = svc.wiki_page("Lonely Node")
    assert out["found"] is True and out["entity"] == "lonely node"
    assert {"kind": "unattributed"} in out["flags"]
```

Note: if `world_write`'s signature differs (check `service.py` around `world_dump`,
service method used by `memory_world_set`), adapt the two calls — the assertion
is what matters: only the matching entity's world facts appear.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wiki_page.py -v`
Expected: the four new tests FAIL with `AttributeError: ... has no attribute 'wiki_page'`.

- [ ] **Step 3: Implement `wiki_page`** (in `service.py`, after `entity_provenance`)

```python
    def wiki_page(self, entity: str, *, mentions_limit: int = 20,
                  timeline_limit: int = 30) -> dict[str, Any]:
        """Everything the console's wiki page needs for one entity, in one
        call: identity + attribution, canonical facts, cited world facts,
        relations (in/out, derived marked), provenance mentions, a merged
        newest-first chronology, and open review flags. Read-only; never
        creates entities and never runs the full review scan."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            e = st.find_entity(G.norm_name(entity))
            if e is None:
                return {"found": False, "entity": entity}
            eid = e["id"]
            projects = st.sources_for_entity(eid)
            community = st.load_communities()["assignment"].get(eid)
            g = st.load_graph()
            mentions = st.entries_for_entity(eid, limit=mentions_limit)
            entity_props = [p for p in st.pending_entity_proposals()
                            if eid in (p.get("entity_id"), p.get("into_id"))]
            edge_props = [p for p in st.pending_proposals()
                          if eid in (p.get("src_id"), p.get("dst_id"))]
            facts = []
            if self._cortex is not None:
                for rec in self._cortex.current_records():
                    if G.norm_name(rec.entity) != e["canonical"]:
                        continue
                    facts.append({
                        "attribute": rec.attribute, "value": rec.value,
                        "confidence": round(float(rec.confidence), 4),
                        "origin": rec.origin, "asserted_at": rec.asserted_at,
                        "history_available": rec.supersedes_value is not None,
                    })
            world_facts = []
            if self._world is not None:
                for rec in self._world.current_records():
                    if G.norm_name(rec.entity) != e["canonical"]:
                        continue
                    world_facts.append({
                        "attribute": rec.attribute, "value": rec.value,
                        "confidence": round(float(rec.confidence), 4),
                        "source_url": rec.source_url,
                        "retrieved_at": rec.retrieved_at,
                    })
        facts.sort(key=lambda f: f["attribute"])
        world_facts.sort(key=lambda f: f["attribute"])

        # Relations via the existing depth-1 neighborhood (derived edges marked,
        # provenance tags included). Outside the lock — it locks itself.
        nb = self.graph_neighborhood(entity, depth=1, include_facts=False)
        rel_out, rel_in = [], []
        for ed in nb.get("edges", []):
            row: dict[str, Any] = {"relation": ed["relation"],
                                   "derived": bool(ed.get("derived"))}
            if row["derived"]:
                row["via"] = ed.get("via")
            else:
                row["confidence"] = ed.get("confidence")
                row["tag"] = ed.get("tag")
            if ed["src"] == e["display"]:
                rel_out.append({**row, "target": ed["dst"]})
            elif ed["dst"] == e["display"]:
                rel_in.append({**row, "source": ed["src"]})

        disp = {en["id"]: en["display"] for en in g["entities"]}
        timeline = [{"ts": float(e["created_at"]), "kind": "entity-created",
                     "text": f"“{e['display']}” first seen"}]
        for ed in g["edges"]:
            if eid in (ed["src_id"], ed["dst_id"]):
                timeline.append({
                    "ts": float(ed["asserted_at"]), "kind": "edge-asserted",
                    "text": (f"{disp.get(ed['src_id'], '?')} {ed['relation']} "
                             f"{disp.get(ed['dst_id'], '?')}")})
        for f in facts:
            timeline.append({"ts": float(f["asserted_at"] or 0.0),
                             "kind": "fact-stamped",
                             "text": f"{f['attribute']} = {f['value']}"})
        for m in mentions:
            timeline.append({"ts": float(m["ts"] or 0.0), "kind": "mention",
                             "text": (m["text"] or "")[:120]})
        timeline.sort(key=lambda t: t["ts"], reverse=True)
        timeline = timeline[:timeline_limit]

        flags: list[dict[str, Any]] = []
        for p in entity_props:
            flags.append({"kind": p["kind"], "id": p["id"],
                          "entity": disp.get(p.get("entity_id")),
                          "into": disp.get(p.get("into_id")),
                          "reason": p.get("reason"), "score": p.get("score")})
        for p in edge_props:
            flags.append({"kind": "proposed_link", "id": p["id"],
                          "src": disp.get(p.get("src_id")),
                          "relation": p.get("relation"),
                          "dst": disp.get(p.get("dst_id")),
                          "confidence": p.get("confidence")})
        if not projects:
            flags.append({"kind": "unattributed"})

        return {"found": True, "entity": e["display"],
                "canonical": e["canonical"], "etype": e["etype"],
                "aliases": e["aliases"], "projects": projects,
                "community": community, "first_seen": float(e["created_at"]),
                "facts": facts, "world_facts": world_facts,
                "relations": {"out": rel_out, "in": rel_in},
                "mentions": mentions, "timeline": timeline, "flags": flags}
```

Verify the world-cortex attribute is `self._world` (used at `service.py:1527`) and
CortexRecord exposes `.origin` (used the same way at `service.py:3731`); both are
existing usage patterns, not new assumptions.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wiki_page.py -v`
Expected: all PASS.

- [ ] **Step 5: Spot-check one load-bearing hook (invalidation-contract discipline)**

Temporarily reverse the `timeline.sort(..., reverse=True)` to `reverse=False`, rerun
`test_wiki_page_timeline_merges_and_orders_newest_first`, confirm it goes RED, revert.
(Proves the ordering test is load-bearing, per project review discipline.)

- [ ] **Step 6: Commit**

```bash
git add tests/test_wiki_page.py pseudolife_memory/service.py
git commit -m "feat(service): wiki_page() one-call entity page assembly"
```

---

### Task 3: Route `GET /api/wiki` + FixtureService support

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (register in the graph section, after `/api/graph/entity-provenance`, ~line 191)
- Modify: `pseudolife_memory/web/fixtures.py` (add `wiki_page` beside `entity_provenance`, ~line 463)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `svc.wiki_page(entity)` (Task 2 signature).
- Produces: `GET /api/wiki?entity=X` → the Task 2 payload; `FixtureService.wiki_page(entity)` returning a representative fixture payload (same keys) for devserver browsing and route tests.

- [ ] **Step 1: Write the failing test** (append to `tests/test_web.py`)

```python
def test_wiki_route_returns_fixture_page(svc):
    routes = ConsoleRoutes(svc)
    handler = routes.table[("GET", "/api/wiki")]
    out = handler({"entity": "daemon"}, None)
    assert out["found"] is True and out["entity"] == "daemon"
    for key in ("aliases", "projects", "facts", "world_facts",
                "relations", "mentions", "timeline", "flags", "first_seen"):
        assert key in out
    assert set(out["relations"]) == {"out", "in"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py::test_wiki_route_returns_fixture_page -v`
Expected: FAIL with `KeyError: ('GET', '/api/wiki')`.

- [ ] **Step 3: Register the route** (routes.py, graph section)

```python
        g("/api/wiki", lambda q, b: svc.wiki_page(_s(q, "entity")))
```

- [ ] **Step 4: Add the fixture** (fixtures.py, beside `entity_provenance`; reuse the
module's existing `time`/`_age` idioms and echo the requested name so any node
click in the devserver UI gets a page)

```python
    def wiki_page(self, entity, mentions_limit=20, timeline_limit=30):
        now = time.time()
        name = entity or "daemon"
        return {
            "found": True, "entity": name, "canonical": name.lower(),
            "etype": "service", "aliases": [f"the {name}"],
            "projects": [{"source": "pseudolife-mcp", "count": 3, "origin": "derived"}],
            "community": 1, "first_seen": now - 86400 * 30,
            "facts": [
                {"attribute": "role", "value": "serves MCP", "confidence": 0.9,
                 "origin": "user", "asserted_at": now - 86400 * 7,
                 "history_available": True},
                {"attribute": "transport", "value": "http", "confidence": 0.8,
                 "origin": "action", "asserted_at": now - 86400 * 2,
                 "history_available": False},
            ],
            "world_facts": [
                {"attribute": "latest-release", "value": "v2.0", "confidence": 0.8,
                 "source_url": "https://example.com/rel", "retrieved_at": now - 3600},
            ],
            "relations": {
                "out": [{"relation": "runs-on", "target": "docker-desktop",
                         "derived": False, "confidence": 0.9, "tag": "EXTRACTED"}],
                "in": [{"relation": "monitors", "source": "watchdog",
                        "derived": True, "via": "rule:monitors"}],
            },
            "mentions": [
                {"id": 42, "band": "slow", "source": "pseudolife-mcp",
                 "ts": now - 86400, "text": f"the {name} runs in docker",
                 "episode_title": "containerization"},
            ],
            "timeline": [
                {"ts": now - 3600, "kind": "fact-stamped", "text": "transport = http"},
                {"ts": now - 86400, "kind": "mention", "text": f"the {name} runs in docker"},
                {"ts": now - 86400 * 30, "kind": "entity-created",
                 "text": f"“{name}” first seen"},
            ],
            "flags": [],
        }
```

- [ ] **Step 5: Run tests to verify they pass (incl. the fixture contract)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py tests/test_fixture_contract.py -v`
Expected: PASS. If `test_fixture_contract.py` fails on the new method, it enforces
signature parity with `MemoryService` — align the fixture's signature to
`wiki_page(self, entity, *, mentions_limit=20, timeline_limit=30)` exactly.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_web.py
git commit -m "feat(web): GET /api/wiki route + fixture page"
```

---

### Task 4: Frontend — wiki panel replaces the node-panel

**Files:**
- Create: `pseudolife_memory/web/static/js/views/wiki_page.js`
- Modify: `pseudolife_memory/web/static/js/views/graph.js` (replace `showNode`, keep everything else)
- Modify: `pseudolife_memory/web/static/css/styles.css` (append panel styles)

**Interfaces:**
- Consumes: `GET /api/wiki?entity=X` (Task 3), existing helpers `el/mount/loadingBlock/errorBlock` (`util.js`), `badge` (`components.js`), `api` (`api.js`), `colorFor` (`graphview.js`).
- Produces: `openWikiPanel(wrap, entityName, { onExplore })` — renders/replaces a `.wiki-panel` inside `wrap`; wikilinks re-open the panel for the clicked entity. Stage 2 will reuse this module unchanged inside the new layout.

- [ ] **Step 1: Create `js/views/wiki_page.js`**

```javascript
// views/wiki_page.js — the live-rendered entity wiki page (spec 2026-07-15 §B).
// Pure render over GET /api/wiki: no LLM, no staleness, read-only.
import { el, mount, loadingBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { badge } from "../components.js";
import { colorFor } from "../graphview.js";

const fmtDate = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "—";

// A clickable entity name: swaps the panel to that entity's page.
function wikilink(name, nav) {
  return el("a", { class: "wikilink", href: "#/graph?entity=" + encodeURIComponent(name),
    onclick: (e) => { e.preventDefault(); nav(name); } }, name);
}

function section(title, ...children) {
  const kids = children.filter(Boolean);
  if (!kids.length) return null;
  return el("div", { class: "wp-section" },
    el("div", { class: "wp-section-title" }, title), ...kids);
}

function factsSection(d) {
  if (!d.facts.length) return null;
  return section("Facts", el("table", { class: "tbl" }, el("tbody", {},
    d.facts.map((f) => el("tr", {},
      el("td", { class: "a" }, f.attribute),
      el("td", {}, f.value,
        f.history_available
          ? el("button", { class: "wp-hist", title: "supersession history",
              onclick: (e) => toggleHistory(e.target, d.canonical, f.attribute) }, "⟲")
          : null),
      el("td", { class: "dim" }, `${f.origin || "—"} · ${f.confidence}`))))));
}

async function toggleHistory(btn, entity, attribute) {
  const row = btn.closest("tr");
  const next = row.nextElementSibling;
  if (next && next.classList.contains("wp-hist-row")) { next.remove(); return; }
  const tr = el("tr", { class: "wp-hist-row" },
    el("td", { colspan: "3" }, loadingBlock("History…")));
  row.after(tr);
  try {
    const h = await api.get("/api/facts/history", { entity, attribute });
    const items = (h.history || h.records || []).map((r) =>
      el("div", { class: "wp-hist-item" },
        el("span", { class: "dim" }, fmtDate(r.asserted_at)), " ", r.value,
        r.status ? el("span", { class: "dim" }, ` (${r.status})`) : null));
    mount(tr.firstChild, items.length ? items : el("span", { class: "dim" }, "no history"));
  } catch (err) { mount(tr.firstChild, errorBlock(err)); }
}

function relationsSection(d, nav) {
  const line = (r, other, arrow) => el("div", { class: "wp-rel" },
    el("span", { class: "mono dim" }, arrow),
    el("span", { class: "mono" }, r.relation), " ",
    wikilink(other, nav), " ",
    r.derived ? badge("derived", "agent") : el("span", { class: "dim" }, String(r.confidence ?? "")));
  return section("Relations",
    ...d.relations.out.map((r) => line(r, r.target, "→")),
    ...d.relations.in.map((r) => line(r, r.source, "←")));
}

function flagBanner(d) {
  if (!d.flags.length) return null;
  return el("div", { class: "wp-flags" }, d.flags.map((f) => {
    if (f.kind === "unattributed")
      return el("div", { class: "chip warn" }, "unattributed — no project owns this entity");
    if (f.kind === "proposed_link")
      return el("div", { class: "chip warn" }, `proposed link: ${f.src} ${f.relation} ${f.dst}`);
    return el("div", { class: "chip warn" },
      `${f.kind}: ${f.entity ?? ""}${f.into ? " → " + f.into : ""}`);
  }));
}

function render(host, d, nav, onExplore) {
  mount(host,
    el("div", { class: "wp-head" },
      el("span", { class: "sw", style: { background: colorFor(d.etype) } }),
      el("h2", {}, d.entity),
      d.etype ? badge(d.etype) : null,
      el("span", { class: "grow" }),
      el("button", { class: "x", title: "close",
        onclick: () => host.closest(".wiki-panel").remove() }, "✕")),
    el("div", { class: "wp-meta dim" },
      d.aliases.length ? `aka ${d.aliases.join(", ")} · ` : "",
      `first seen ${fmtDate(d.first_seen)}`,
      d.community != null ? ` · community ${d.community}` : "",
      d.projects.length ? ` · ${d.projects.map((p) => p.source).join(", ")}` : ""),
    flagBanner(d),
    factsSection(d),
    d.world_facts.length ? section("World", d.world_facts.map((w) =>
      el("div", { class: "wp-world" }, el("span", { class: "a" }, w.attribute), " ", w.value, " ",
        w.source_url ? el("a", { href: w.source_url, target: "_blank", rel: "noopener noreferrer" }, "source") : null))) : null,
    relationsSection(d, nav),
    d.mentions.length ? section("Mentions", d.mentions.map((m) =>
      el("div", { class: "wp-mention" },
        el("span", { class: "dim" }, `${fmtDate(m.ts)} · ${m.source}`),
        el("div", {}, m.text)))) : null,
    d.timeline.length ? section("Timeline", d.timeline.map((t) =>
      el("div", { class: "wp-tl" },
        el("span", { class: "dim mono" }, fmtDate(t.ts)),
        el("span", { class: "wp-tl-kind" }, t.kind), " ", t.text))) : null,
    el("div", { class: "wp-actions" },
      el("button", { class: "btn sm primary", onclick: () => onExplore(d.entity) }, "Explore from here"),
      el("button", { class: "btn sm", onclick: () => {
        location.hash = "#/cortex?q=" + encodeURIComponent(d.entity); } }, "Facts ↗")));
}

// Open (or refresh) the wiki panel inside `wrap` for `entityName`.
export function openWikiPanel(wrap, entityName, { onExplore } = {}) {
  let panel = wrap.querySelector(".wiki-panel");
  if (!panel) { panel = el("div", { class: "wiki-panel" }); wrap.appendChild(panel); }
  const host = el("div", { class: "wp-body" });
  mount(panel, host);
  mount(host, loadingBlock("Opening page…"));
  const nav = (name) => openWikiPanel(wrap, name, { onExplore });
  api.get("/api/wiki", { entity: entityName }).then((d) => {
    if (!host.isConnected) return;
    if (!d.found) { mount(host, errorBlock(new Error(`no page for “${entityName}”`))); return; }
    render(host, d, nav, onExplore || (() => {}));
  }).catch((err) => { if (host.isConnected) mount(host, errorBlock(err)); });
}
```

Adapt the history response shape (`h.history || h.records`) to whatever
`svc.history()` actually returns — check `FixtureService.history` (fixtures.py:184)
and assert the real key; do not ship the `||` guess.

- [ ] **Step 2: Wire into `views/graph.js`**

Replace the `showNode(wrap, node)` function body (graph.js:128-147) with:

```javascript
  function showNode(wrap, node) {
    openWikiPanel(wrap, node.entity, { onExplore: (id) => goExplore(id) });
  }
```

Add the import at the top: `import { openWikiPanel } from "./wiki_page.js";`
Remove now-unused imports if any (`badge`, `colorFor` stay only if still used elsewhere in the file).
The galaxy's `onNodeClick` (graph.js:98) still routes to `goExplore` — leave it for
Stage 1; the galaxy gets the panel in Stage 2.

- [ ] **Step 3: Panel styles** (append to `styles.css`; reuse the existing
`.node-panel` block as the starting point — same tokens, wider)

```css
/* ── wiki panel (Atlas stage 1) ─────────────────────────────────────────── */
.wiki-panel {
  position: absolute; top: 12px; right: 12px; bottom: 12px; width: min(42%, 460px);
  overflow-y: auto; border-radius: 10px; padding: 14px 16px;
  background: var(--panel-bg, rgba(20, 24, 32, .92));
  border: 1px solid var(--panel-border, rgba(150, 170, 200, .18));
  backdrop-filter: blur(6px); z-index: 5;
}
[data-theme="light"] .wiki-panel { background: rgba(252, 250, 246, .95); }
.wp-head { display: flex; align-items: center; gap: 8px; }
.wp-head h2 { margin: 0; font-size: 1.05rem; overflow-wrap: anywhere; }
.wp-head .sw { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
.wp-meta { font-size: .8rem; margin: 4px 0 10px; }
.wp-section { margin: 12px 0; }
.wp-section-title { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
  opacity: .65; margin-bottom: 6px; }
.wp-rel, .wp-mention, .wp-tl, .wp-world { font-size: .86rem; margin: 4px 0; }
.wp-tl-kind { font-size: .7rem; opacity: .6; margin: 0 6px; }
.wp-flags { margin: 8px 0; display: flex; flex-direction: column; gap: 4px; }
.wp-actions { display: flex; gap: 8px; margin-top: 14px; }
.wp-hist { margin-left: 6px; }
.wikilink { color: var(--accent, #5b9dff); text-decoration: none; border-bottom: 1px dotted; }
```

Check `styles.css` for the actual `.node-panel` variable names (`--panel-bg` etc.)
and use the file's real tokens — the values above are defaults, the tokens must
match the existing sheet.

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/web/static/js/views/wiki_page.js pseudolife_memory/web/static/js/views/graph.js pseudolife_memory/web/static/css/styles.css
git commit -m "feat(console): wiki panel replaces the graph node-panel"
```

---

### Task 5: QA, full suite, CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]`, dated subsection per house style)

**Interfaces:**
- Consumes: everything above; `devserver.py` + `FixtureService` for offline browsing.

- [ ] **Step 1: Browser QA against the fixture devserver**

Start: `.venv/Scripts/python.exe -m pseudolife_memory.web.devserver` (check the
module's `__main__` for the exact invocation/port; it serves the console over
`FixtureService`). In the browser: open `#/graph`, click a node on the 2D map →
wiki panel opens with the fixture page; click the `docker-desktop` wikilink →
panel swaps; expand a fact history; check flags banner absent (fixture has none);
`Explore from here` still enters explore mode; close button works; repeat in
light theme; panel scrolls on a short window; `#/graph?entity=daemon` deep link
still lands in explore mode with the panel available on node click.

- [ ] **Step 2: Full suite with bench Postgres up**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/` (bench PG at
127.0.0.1:5433 — verify PG-backed tests RAN, not skipped: `-rs` and grep the skip
report for `pg`).
Expected: all pass, wiki tests included in the run count.

- [ ] **Step 3: CHANGELOG entry** (under `[Unreleased]`, matching existing style)

```markdown
### 2026-07-15 — Atlas stage 1: entity wiki pages
- New `GET /api/wiki?entity=X`: one-call, live-rendered entity page — identity,
  project attribution, canonical facts (with history), cited world facts,
  relations, provenance mentions, merged timeline, open review flags.
- Console: clicking a graph node now opens the wiki panel (replaces the small
  node-panel); wikilinks browse between entity pages in place.
- Storage: `find_entity`/`load_graph` expose `entities.created_at` (additive).
```

- [ ] **Step 4: Review pass, then commit**

Run a `/code-review` medium pass (project discipline for console/perf-adjacent
changes), address findings, then:

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Atlas stage 1 wiki spine"
```

---

## Self-review notes

- **Spec §B coverage:** identity/aliases/projects/first-seen ✔ (Task 2), facts +
  lazy history ✔ (Tasks 2/4), world facts + citations ✔, relations in/out with
  derived marking ✔, mentions ✔, timeline ✔, flags without full scan ✔
  (proposals + unattributed only — the dubious-edge flag arrives with Stage 3's
  review contextualization, where the pulse/banner UX lands), `found:false` ✔,
  wikilinks ✔ (camera fly-to is Stage 2, per stage split).
- **Types:** `openWikiPanel(wrap, name, {onExplore})` used identically in Tasks 4;
  `wiki_page` signature identical in service, fixture, route.
- **Verify-don't-assume markers:** three explicit check-the-real-shape
  instructions (world_write signature, history payload key, CSS tokens) — each
  bounded to one file the implementer already has open.
