# Granular (per-item) bulk actions in the Atlas review queue — design

- Date: 2026-06-29
- Status: approved (brainstorm); pending spec review
- Area: `pseudolife_memory/web/static/js/atlas_review.js` (+ `views/atlas.js`, `web/fixtures.py`)

## Problem

The review queue's group findings act all-or-nothing: one click prunes **all ~192**
low-confidence edges (`dubious_edge`), assigns **all ~211** unattributed entities to a
single project (`unattributed`), or deletes all `test_artifact` entities — and the
`orphan` finding (~746 weakly-connected entities) has **no action at all**. The panel
also truncates the display to 8/6 items, so the full set isn't even visible. There's no
way to act on a chosen subset.

## Decisions (from brainstorm)

1. **Scope** — per-item selection for `dubious_edge` (Prune), `unattributed` (Assign),
   `test_artifact` (Delete), and make `orphan` actionable with **Delete + Assign**.
2. **Interaction** — an inline, capped-height, scrollable checkbox list per finding with
   a filter box, "select all (filtered)" / "clear", and a live count on the action
   button(s). **Default selection is empty** (opt-in); "prune most" = select-all → deselect
   a few.

## Design

**No backend change.** The findings already carry their complete lists
(`dubious_edge.edges`, `unattributed.entities`, `test_artifact.entities`,
`orphan.entities`), and the `atlas.js` `prune` / `assign` / `delete-names` handlers
already `postAll` over whatever array they receive. This is a render + wiring change.

### Component 1 — `selectableList` (`atlas_review.js`)

A reusable selectable list:

```
selectableList(items, { cell, filterText, rowExtra, onChange }) -> { node, getSelected }
```

- `items`: the finding's array (edge objects or entity-name strings).
- `cell(item) -> Element`: per-row content after the checkbox (the clickable provenance
  chip for entities; an edge-label span for edges).
- `filterText(item) -> string`: lowercased text the filter matches against.
- `rowExtra(item) -> Element | null`: optional element stacked under the row (the entity's
  provenance drawer).
- `onChange(count)`: fired whenever the selection changes (drives the button label/disabled).
- Returns `node` (filter input + `select all (filtered)` / `clear` + a `max-height:~280px;
  overflow:auto` row container) and `getSelected()` (the selected items, in list order).

Behaviour: a `Set` tracks selected items; the filter hides non-matching rows; "select all
(filtered)" adds every currently-visible item; "clear" empties the set. Default: empty.

### Component 2 — `findingRow` wiring (`atlas_review.js`)

For the four findings, replace the static chip list + single bulk button with a
`selectableList` plus action button(s) that read `getSelected()`:

| Finding | `cell` | `filterText` | Buttons (act on selection) |
|---|---|---|---|
| `dubious_edge` | edge-label span `src —rel→ dst (conf)` | `"src rel dst"` | **Prune** → `{kind:"prune", edges}` |
| `unattributed` | `entityRef(name).chip` | name | **Assign** → `{kind:"assign", entities}` |
| `test_artifact` | `entityRef(name).chip` | name | **Delete** → `{kind:"delete-names", entities}` |
| `orphan` | `entityRef(name).chip` | name | **Delete** + **Assign** (same kinds) |

Each button shows the live count (`Prune (12)`) and is disabled at 0 selected. Entity
rows keep the existing `entityRef` provenance drawer (`rowExtra`). The other finding types
(`merge_candidate`, `junk_candidate`, `proposed_link`, `duplicate`) are unchanged.

### Component 3 — `views/atlas.js`

Essentially unchanged: the `prune` / `assign` / `delete-names` handlers already iterate
`d.edges` / `d.entities`, so they now receive the *selected subset*; the confirm dialogs
already interpolate `array.length`, so they read e.g. "Remove 12 edges?". `orphan` reuses
the existing `delete-names` and `assign` kinds — no new handler. (Add a no-op guard if the
selection is empty, though the disabled button already prevents that.)

### Component 4 — fixtures (`web/fixtures.py`)

Enrich `FixtureService.graph_review` so the devserver exercises selection: give
`dubious_edge` ~12 edges and add an `orphan` finding with several entities (and keep
`unattributed` with a handful). `counts.total` already derives from `len(findings)`.

## Test plan

No JS test runner — verify on the fixture devserver (`:8770`) via chrome-devtools:
- filter narrows the list; "select all (filtered)" / "clear" work;
- the action button shows the live selected count and is disabled at 0;
- **Prune acts on exactly the selected subset** (network shows N `unrelate` calls for N
  selected, not the full list);
- `orphan` offers Delete + Assign on the selection;
- entity rows still open their provenance drawer; zero console errors.
- Backend `test_web.py` stays green (fixture `graph_review` shape unchanged except more items).

## Risks / mitigations

- **Long lists are heavy DOM** (~750 rows) → cap the container height and rely on the
  filter; rows are lightweight (checkbox + span). Acceptable for a review tool.
- **Accidental large prune/delete** → opt-in default + the existing danger confirm dialog
  (now showing the real selected count) keep it safe.
- **Selection lost on re-fetch** → after a mutation the panel re-fetches and rebuilds (the
  acted items are gone); selection resets, which is the desired behaviour.
