# Cortex Console v2 — Phase 1 (Close the Backend Gap) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the backend capabilities the console doesn't expose yet — graph-insight digest, communities, path, multi-hop recall, consolidation review, engram traces — and the safe write actions.

**Architecture:** Extend the existing daemon-served vanilla-ESM SPA + thin enumerated REST over `service.*`. New endpoints are individually enumerated (no generic proxy); reads are GET, mutations POST; `/api` stays token-gated. New views/drawers are self-contained ES modules following the existing `views/*.js` pattern. Fixtures (`FixtureService`) gain matching methods so the devserver + tests cover the new surface.

**Tech Stack:** Vanilla ES modules + hand-rolled CSS. Python 3.11 ASGI. Tests: pytest (`tests/test_web.py`, FixtureService pattern) for backend; chrome-devtools MCP against the fixture devserver for frontend.

## Global Constraints

- **No build step, no CDN, fully offline.** Vanilla ES modules only.
- **Run Python under `.venv`** (`.venv/Scripts/python.exe`) — FixtureService imports torch transitively.
- **Devserver:** `.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8770` → `http://127.0.0.1:8770/ui/`. Restart it after editing Python (routes/fixtures); a browser **full reload** (not hash nav) is required to pick up edited JS/CSS.
- **Endpoint contract:** enumerated handler per route, 1:1 over `service.*`, GET=read/POST=mutate, token-gated, no generic proxy.
- **Mutations are confirm-gated + danger-styled** in the UI (preserve "read-mostly, safe by construction").
- **Commit style:** conventional commits; end each message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Branch:** `feat/cortex-console-v2-p1` (already created off master).

## Verified service signatures & shapes (do not re-derive)

```
graph_digest()            -> {available: bool, digest?: {computed_at, communities:[{id,label,size,cohesion}],
                              god_nodes:[{entity_id,display,degree}],
                              surprises:[{src,dst,relation,confidence,origin,score,why}],
                              questions:[{type,question,why}],
                              totals:{entities,edges,communities}}, reason?: str}
communities(community_id?) -> {communities:[{id,label,size,cohesion}]} | {community_id, members:[str]}
graph_path(source,target,max_hops=8) -> {found, path:[str], edges:[{src,relation,dst}], hops, source, target} | {found:False, missing}
recall(query,hops?,top_k?)-> {query, seeds:[str], entities:[{entity,facts:[]}], edges:[{src,relation,dst,derived}],
                              paths:[[str]], texts:[str], iterations, hops, low_confidence}
get_entry(entry_id)       -> {found, entry_id, text, source, reinforcements, access_count, consolidated_into:[facts]} | {found:False, faded:True}
reinforce(entry_id)       -> dict
consolidation_candidates(...) -> {clusters:[{cohesion, seed_score, size, members:[entry]}]}
consolidate(replaces,new_text,...) -> {superseded_count, new_memory_stored, ...}
cortex_write/cortex_forget/supersede/delete -> already wired in routes.py and FixtureService
```

---

### Task 1: New REST endpoints + fixture methods + tests (backend, TDD)

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (register 5 routes in `_register`)
- Modify: `pseudolife_memory/web/fixtures.py` (add `graph_digest`, `communities`, `graph_path`, `get_entry`, `reinforce`)
- Modify: `tests/test_web.py` (dispatch tests; fix stale "no torch" docstring)

**Interfaces:**
- Produces: routes `GET /api/graph/digest`, `GET /api/graph/communities[?id=]`, `GET /api/graph/path?source=&target=&max_hops=`, `GET /api/entry?id=`, `POST /api/reinforce` (body `{entry_id}`). Frontend tasks 2–5 consume these.

- [ ] **Step 1: Write failing dispatch tests** in `tests/test_web.py` (after `test_routes_dispatch_reads`):

```python
def test_routes_graph_insight_dispatch(svc):
    r = ConsoleRoutes(svc)
    dig = r.dispatch("GET", "/api/graph/digest", {}, {})
    assert "available" in dig
    comms = r.dispatch("GET", "/api/graph/communities", {}, {})
    assert "communities" in comms
    path = r.dispatch("GET", "/api/graph/path", {"source": "a", "target": "b"}, {})
    assert "found" in path and "path" in path


def test_routes_entry_and_reinforce(svc):
    r = ConsoleRoutes(svc)
    entry = r.dispatch("GET", "/api/entry", {"id": "1"}, {})
    assert "consolidated_into" in entry and "reinforcements" in entry
    out = r.dispatch("POST", "/api/reinforce", {}, {"entry_id": 1})
    assert isinstance(out, dict)
```

- [ ] **Step 2: Run, verify they fail** — `KeyError` (routes unregistered):

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py::test_routes_graph_insight_dispatch tests/test_web.py::test_routes_entry_and_reinforce -q`
Expected: FAIL (KeyError on the new paths).

- [ ] **Step 3a: Add an optional-int helper** at module level in `routes.py` (near `_i`):

```python
def _i_opt(params: dict, key: str) -> int | None:
    v = params.get(key)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 3b: Register the graph-insight routes** in `_register`, in the `# ---- graph ----` block (after the existing `/api/graph`):

```python
        g("/api/graph/digest", lambda q, b: svc.graph_digest())
        g("/api/graph/communities", lambda q, b: svc.communities(community_id=_i_opt(q, "id")))
        g("/api/graph/path", lambda q, b: svc.graph_path(
            _s(q, "source"), _s(q, "target"), max_hops=_i(q, "max_hops", 8)))
```

- [ ] **Step 3c: Register the engram routes** in a new `# ---- engram traces / retention ----` block (after `/api/recall`):

```python
        g("/api/entry", lambda q, b: svc.get_entry(_i(q, "id", 0)))
        p("/api/reinforce", lambda q, b: svc.reinforce(int(b["entry_id"])))
```

- [ ] **Step 4: Add fixture methods** to `FixtureService` in `fixtures.py` (after `graph_neighborhood`, before `# dream / consolidation`):

```python
    # graph insight
    def graph_digest(self):
        return {"available": True, "digest": {
            "computed_at": _NOW - 40 * 60,
            "communities": [
                {"id": 0, "label": "pseudolife-mcp", "size": 12, "cohesion": 0.42},
                {"id": 1, "label": "Cortex Console web frontend", "size": 9, "cohesion": 0.55},
                {"id": 2, "label": "postgres", "size": 4, "cohesion": 0.31}],
            "god_nodes": [
                {"entity_id": 1, "display": "pseudolife-mcp", "degree": 12},
                {"entity_id": 2, "display": "Cortex Console web frontend", "degree": 10},
                {"entity_id": 3, "display": "postgres", "degree": 6}],
            "surprises": [
                {"src": "claude-code", "dst": "pseudolife-mcp", "relation": "writes-to",
                 "confidence": 0.85, "origin": "agent", "score": 5,
                 "why": "agent-inferred; bridge between community 1 and 2"}],
            "questions": [
                {"type": "contested_fact", "question": "Which value of `model` for `dream extractor` is correct — `Gemma 4 E2B` or `Gemma 4 E4B`?", "why": "Contested fact; rival from origin=agent."},
                {"type": "isolated_entity", "question": "What connects `Auth flow` to the rest of the graph?", "why": "1 weakly-connected entity — possible gap."}],
            "totals": {"entities": 25, "edges": 26, "communities": 3}}}

    def communities(self, community_id=None):
        comms = [{"id": 0, "label": "pseudolife-mcp", "size": 12, "cohesion": 0.42},
                 {"id": 1, "label": "Cortex Console web frontend", "size": 9, "cohesion": 0.55},
                 {"id": 2, "label": "postgres", "size": 4, "cohesion": 0.31}]
        if community_id is None:
            return {"communities": comms}
        return {"community_id": community_id,
                "members": ["pseudolife-mcp", "postgres", "docker-desktop"]}

    def graph_path(self, source, target, max_hops=8):
        return {"found": True, "source": source, "target": target, "hops": 2,
                "path": [source or "pseudolife-mcp", "postgres", target or "docker-desktop"],
                "edges": [{"src": source or "pseudolife-mcp", "relation": "stores-data-in", "dst": "postgres"},
                          {"src": "postgres", "relation": "runs-on", "dst": target or "docker-desktop"}]}

    def get_entry(self, entry_id):
        return {"found": True, "entry_id": int(entry_id),
                "text": _STREAM[2][0], "source": "pseudolife",
                "reinforcements": 2, "access_count": 13,
                "consolidated_into": [
                    {"entity": "pseudolife-mcp", "attribute": "graph population", "value": "GAM #2 live"}]}

    def reinforce(self, entry_id):
        return {"reinforced": True, "entry_id": int(entry_id), "reinforcements": 3}
```

- [ ] **Step 5: Run the new tests + full web suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py -q`
Expected: PASS (27+).

- [ ] **Step 6: Fix the stale docstring** in `tests/test_web.py` (top of file): change the line claiming "no Postgres, no torch" to note FixtureService now imports torch transitively and the web tests run under `.venv`.

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_web.py
git commit -m "$(printf 'feat(web): graph digest/communities/path + entry/reinforce endpoints\n\nFive enumerated routes over graph_digest/communities/graph_path/get_entry/\nreinforce, with matching FixtureService methods and dispatch tests. Fix the\nstale no-torch docstring.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: Insight view (new tab, Structure)

**Files:**
- Create: `pseudolife_memory/web/static/js/views/insight.js`
- Modify: `pseudolife_memory/web/static/js/app.js` (import + ROUTES entry under Structure, after graph)
- Modify: `pseudolife_memory/web/static/css/styles.css` (insight blocks)

**Interfaces:**
- Consumes: `GET /api/graph/digest`. `el`, `mount`, `loadingBlock`, `emptyBlock`, `errorBlock`, `fmtNum`, `fmtAge` from util; `panel`/`badge` from components.
- Produces: `renderInsight(root, ctx)`.

- [ ] **Step 1: Write the view.** `insight.js` fetches `/api/graph/digest`; if `!available`, show `emptyBlock("No graph digest yet", "Run a dream to build the graph insight.")`. Otherwise render, in this order (golden-signals: most actionable first):
  1. **Totals** eyebrow row (`digest.totals`: entities · edges · communities) + `computed_at` via `fmtAge`.
  2. **Suggested questions** panel (accent `var(--c-cortex)`): each `q` a card with `q.question` (display) + `q.why` (dim) + a `badge(q.type)`. These are the actionable signal.
  3. **God-nodes** panel (accent `var(--c-graph)`): horizontal degree bars — for each `{display, degree}`, a row with label + a bar width `degree/maxDegree*100%` (reuse `.conf .bar` styling or a new `.gn-bar`); clicking a god-node sets `location.hash = "#/graph?entity=" + encodeURIComponent(display)`.
  4. **Communities** panel (accent `var(--c-graph)`): table of `{label, size, cohesion}`; clicking a row → `#/graph?entity=` + label.
  5. **Surprises** panel (accent `var(--c-lessons)`): each `{src, relation, dst, why, score}` a card showing `src —relation→ dst` (mono) + `why` (dim) + score pill.

- [ ] **Step 2: Register the route** in `app.js`. Add import `import { renderInsight } from "./views/insight.js";` and insert into ROUTES right after the `graph` entry:

```js
  { id: "insight", label: "Insight", group: "Structure", accent: "var(--c-graph)", view: renderInsight, countKey: null },
```

- [ ] **Step 3: Add CSS** for `.gn-bar` (degree bar) and `.surprise-card` / question cards (reuse `.entry`, `.kv`, `.score-pill` where possible to stay DRY).

- [ ] **Step 4: Verify in the browser.** Restart devserver. Full-reload `http://127.0.0.1:8770/ui/#/insight`. `evaluate_script` asserting the digest sections render (questions count ≥ 1, god-node bars present); `take_screenshot` dark + light. Confirm a god-node click routes to `#/graph?entity=…`.

- [ ] **Step 5: Commit** (`feat(web): Insight tab — graph digest (god-nodes, questions, surprises, communities)`).

---

### Task 3: Recall view (new tab, Memory)

**Files:**
- Create: `pseudolife_memory/web/static/js/views/recall.js`
- Modify: `app.js` (import + ROUTES entry under Memory, after stream)
- Modify: `styles.css` (recall chain styling)

**Interfaces:**
- Consumes: `GET /api/recall?q=&hops=`, `GET /api/graph/path?source=&target=`. util + components helpers.
- Produces: `renderRecall(root, ctx)`.

- [ ] **Step 1: Write the view.** Two modes via a `facetBar`: **"multi-hop"** (default) and **"path between two"**.
  - Multi-hop: a `searchBox` + a hops `select` (1–5). On submit → `/api/recall`. Render: a `low_confidence` warn chip when true; the **seeds** as chips; the **paths** as chains — each path array rendered `a —→ b —→ c` (mono, using `.recall-chain`); the **entities** as cards (entity name + its facts via existing fact styling); the **texts** as `.entry` cards.
  - Path mode: two inputs (source, target) + Explore → `/api/graph/path`. Render the returned `edges` as a chain `src —relation→ dst`; `found:false`/empty path → emptyBlock.
- [ ] **Step 2: Register the route** in `app.js`: import `renderRecall`, insert after `stream`:

```js
  { id: "recall", label: "Recall", group: "Memory", accent: "var(--c-assoc)", view: renderRecall, countKey: null },
```

- [ ] **Step 3: Add CSS** `.recall-chain` (flex row, mono, arrow separators) + reuse `.entry`/`.chip`.
- [ ] **Step 4: Verify in the browser.** Restart devserver, full-reload `#/recall`, submit a query → chains/seeds/entities render; switch to path mode → path renders. Screenshot dark/light.
- [ ] **Step 5: Commit** (`feat(web): Recall tab — multi-hop recall + path-between-two`).

---

### Task 4: Consolidation review drawer

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/observatory.js` (add a "Review consolidation" button in `dreamPanel`)
- Create: `pseudolife_memory/web/static/js/consolidation.js` (the drawer logic, reused from observatory)

**Interfaces:**
- Consumes: `GET /api/consolidation`, `POST /api/consolidate` (body `{replaces:[texts], new_text}`). `openDrawer`, `setDrawerBody`, `confirmDialog`, `toast` from ui.
- Produces: `openConsolidationDrawer(ctx)`.

- [ ] **Step 1: Write the drawer.** `openConsolidationDrawer` opens a drawer (accent `var(--c-lessons)`), fetches `/api/consolidation`, lists each cluster: cohesion + size header, members as `.entry` cards. Each cluster has a **Consolidate** button → a modal previews the members and a textarea pre-filled with a suggested merged text; on confirm POST `/api/consolidate` `{replaces: member texts, new_text}`, toast the result, refresh.
- [ ] **Step 2: Wire the button** into `observatory.js` `dreamPanel` — next to "Run dream", a `btn sm` "Review consolidation" calling `openConsolidationDrawer(ctx)`.
- [ ] **Step 3: Verify in the browser.** Restart devserver, full-reload `#/observatory`, click Review consolidation → drawer lists the fixture cluster; Consolidate → preview modal → confirm → success toast.
- [ ] **Step 4: Commit** (`feat(web): consolidation review drawer from the dream panel`).

---

### Task 5: Engram trace drawer + reinforce (Stream)

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/stream.js` (add a trace affordance per entry)
- Modify: `pseudolife_memory/web/fixtures.py` (add an `id` to `_stream_dict`)

**Interfaces:**
- Consumes: `GET /api/entry?id=`, `POST /api/reinforce`. `openDrawer`, `setDrawerBody`, `toast`.
- Produces: drawer opened from a Stream entry.

- [ ] **Step 1: Give fixture stream entries an id.** In `_stream_dict`, add `"id": 1000 + idx` to the returned dict (real bank entries already carry an id).
- [ ] **Step 2: Add the trace affordance** in `stream.js` `entryCard`: when `e.id != null`, add a small "trace ↗" button in `entry-meta` that opens a drawer (accent `var(--c-assoc)`) loading `/api/entry?id=e.id`: show text, source, `reinforcements`/`access_count` chips, `consolidated_into` facts (using existing fact row styling), and a **Reinforce** button → `POST /api/reinforce {entry_id:e.id}` → toast + update the count.
- [ ] **Step 3: Verify in the browser.** Restart devserver, full-reload `#/stream`, click trace on an entry → drawer shows reinforcements + consolidated_into; Reinforce → toast.
- [ ] **Step 4: Commit** (`feat(web): engram trace drawer + reinforce on stream entries`).

---

### Task 6: Write actions — assert/correct, forget, supersede, delete (confirm-gated)

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/cortex.js` (assert/correct + forget)
- Modify: `pseudolife_memory/web/static/js/views/stream.js` (supersede + delete)

**Interfaces:**
- Consumes: `POST /api/facts/set` `{entity,attribute,value,confidence,origin}`, `POST /api/facts/forget` `{entity,attribute}`, `POST /api/supersede` `{old_text,new_text}`, `POST /api/delete` `{text}`. `openModal`, `confirmDialog`, `toast`.

- [ ] **Step 1: Cortex assert/correct.** In `cortex.js` `entityCard`, add a `btn sm` "+ fact" in the entity head opening a modal (attribute, value, origin select user/action/agent, confidence number) → `POST /api/facts/set`; on success toast + `ctx.refresh()`.
- [ ] **Step 2: Cortex forget.** In `factRow`, add a small danger affordance (e.g. a "forget" link in `fact-side`) → `confirmDialog({danger:true})` → `POST /api/facts/forget {entity, attribute}` → toast + refresh. Ensure `e.stopPropagation()` so it doesn't open the history drawer.
- [ ] **Step 3: Stream supersede + delete.** In `stream.js` `entryCard`, add (behind a small "⋯" affordance to avoid clutter) "supersede" (modal: new text → `POST /api/supersede {old_text:e.text, new_text}`) and "delete" (`confirmDialog danger` → `POST /api/delete {text:e.text}`). Both toast + reload the list.
- [ ] **Step 4: Verify in the browser.** Restart devserver, full-reload; exercise each action against fixtures (they return canned success), assert toasts fire and no console errors. Confirm destructive actions show a confirm dialog first.
- [ ] **Step 5: Commit** (`feat(web): confirm-gated write actions (assert/forget/supersede/delete)`).

---

## Phase 1 exit check

- [ ] `.venv/Scripts/python.exe -m pytest tests/test_web.py -q` passes (existing + new dispatch tests).
- [ ] Browser pass at `http://127.0.0.1:8770/ui/`: zero console errors; Insight + Recall tabs render under their groups; consolidation + engram drawers open; write actions are confirm-gated and toast.
- [ ] Phase 1 is additive (no schema change) — deployable via the established daemon-rebuild + `backup.ps1`-first procedure when the user is present.
