# Vendored third-party assets

These files are vendored (committed in-tree) so the Cortex Console stays fully
**offline** — no CDN at runtime, matching the daemon's baked `HF_HUB_OFFLINE`
image. They are served verbatim at `/ui/vendor/` and lazy-loaded on demand.

## `galaxy.bundle.js`

- **What:** [`3d-force-graph`](https://github.com/vasturiano/3d-force-graph)
  (WebGL 3D force-directed graph) bundled together with its own
  [three.js](https://github.com/mrdoob/three.js) so the console can build
  custom scene objects (label sprites, community nebulae) against the **same
  three instance** the graph uses — two three copies in one scene is the
  classic `instanceof`/render-state landmine this bundle exists to avoid.
- **Exports:** `default` → the `ForceGraph3D` constructor; `THREE` → the full
  three namespace. Powers the Graph tab's galaxy via `static/js/galaxy.js`.
- **Versions (pinned):** `3d-force-graph@1.73.6`, `three@0.185.1` (resolved,
  single deduped copy — verified with `npm ls three`).
- **Size:** ≈1.5 MB minified ESM, target es2020.

### Build recipe (reproducible)

```bash
mkdir galaxy-bundle && cd galaxy-bundle && npm init -y
npm install --save-exact 3d-force-graph@1.73.6
npm install --save-dev --save-exact esbuild@0.24.2 license-checker-rseidelsohn@4.4.2

# license gate — allowlist: MIT/ISC/BSD-2/BSD-3/Apache-2.0/0BSD/CC0/Unlicense
npx license-checker-rseidelsohn --production --summary
npm ls three          # must resolve to exactly ONE copy

cat > entry.mjs <<'EOF'
import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
export default ForceGraph3D;
export { THREE };
EOF
npx esbuild entry.mjs --bundle --format=esm --target=es2020 --minify \
  --legal-comments=eof --outfile=galaxy.bundle.js

# self-containment checks
grep -c  WebGLRenderer galaxy.bundle.js          # > 0
grep -cE 'from ?"(https?:|/)' galaxy.bundle.js   # must be 0
```

To update: bump the pinned version, rerun the recipe (the license gate and the
one-three check are mandatory, not advisory), re-run the galaxy QA.

### License inventory (audit of 2026-07-15, all permissive)

`license-checker-rseidelsohn --production` summary: **MIT ×18, ISC ×14,
BSD-3-Clause ×4** — nothing copyleft, nothing unknown. Upstream `@license`
banners are embedded at the end of the bundle (`--legal-comments=eof`).

- **MIT:** 3d-force-graph, three, three-forcegraph, three-render-objects,
  d3-force-3d, d3-binarytree, d3-octree, @tweenjs/tween.js, @babel/runtime,
  accessor-fn, data-bind-mapper, float-tooltip, kapsule, lodash-es,
  ngraph.merge, polished, preact, tinycolor2
- **ISC:** d3-array, d3-color, d3-dispatch, d3-format, d3-interpolate,
  d3-quadtree, d3-scale, d3-scale-chromatic, d3-selection, d3-time,
  d3-time-format, d3-timer, internmap
- **BSD-3-Clause:** ngraph.events, ngraph.forcelayout, ngraph.graph,
  ngraph.random

Attribution: `3d-force-graph` © Vasco Asturiano (MIT); `three.js` © three.js
authors (MIT); d3 modules © Mike Bostock (ISC); ngraph © Andrei Kashcha
(BSD-3-Clause).

## `3d-force-graph.bundle.js` (legacy — removal pending)

The previous esm.sh bundle (`3d-force-graph@1.73.6`, MIT) that exported only
the default constructor. Superseded by `galaxy.bundle.js`; deleted once
nothing imports it (Atlas stage 2, task 4).
