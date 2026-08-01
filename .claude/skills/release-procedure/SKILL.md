---
name: release-procedure
description: Use when cutting a release or publishing anything to a public surface — GitHub release, PyPI, the MCP registry, or the Claude Code plugin marketplace. Covers the docs currency pass, the six-file version cut, build/inspect, the Trusted Publishing + registry automation, and verification.
---

# Release / publish procedure (four public surfaces)

GitHub releases, PyPI, the MCP registry, and the Claude Code plugin
marketplace (`.claude-plugin/marketplace.json` + `plugin/` — served straight
from master, no publish step; users pull via `/plugin marketplace update`)
all serve from this repo; a release touches them in this order (first done
2026-07-16, v0.8.0).

0. **Docs currency pass before the cut** — two checks, and (a) is the one
   that gets skipped because nothing fails when you miss it.

   **(a) Absence — is the new behavior documented at all?** List the
   behavior changes since the last tag (`git log vN.N.N..HEAD`, the
   CHANGELOG's `[Unreleased]`) and ask of each: *which user-facing page
   describes this?* A capability with no guide entry contradicts nothing,
   so no guard test and no re-verify pass will ever surface it — only this
   question will. Schema v16–v18 shipped undocumented exactly this way, and
   0.10.0's headline extraction change lived in the CHANGELOG alone until
   it was caught by hand at the release gate. A CHANGELOG entry is a record
   of a change, not documentation of a behavior. Corollary: never exclude a
   file from this pass on the strength of an earlier read that was looking
   for something else — a narrow check does not justify a broad exclusion.

   **(b) Contradiction — do the existing claims still hold?** The guard
   tests pin numbers (schema, identifiers), but *framing* drifts silently:
   the 2026-07-16 pass found 15 stale claims the guards can't see.
   Re-verify the drift-prone claim classes against code before any release:
   what's bundled/default (extractor model + size, embedding weights),
   the transport story (HTTP-first; shim = host-process only), lifecycle
   ownership (episodes are daemon-owned; briefing is the only hook),
   tool count/tiers and the hidden-tools-need-expand rule, shipped config
   defaults (surprise gate is permissive), image/install sizes, and any
   "can't / doesn't / no X" absolute — those age worst. **Translated front
   doors** (`docs/i18n/README.{zh,ja,ko,pt-br,es}.md`): if the narrative or
   quickstart in `docs/i18n/README.source.md` changed, bump its
   `i18n-source` version and re-run the translation subagents — the guard
   (`tests/test_i18n_readme.py`) pins code blocks + sync markers but cannot
   read prose. Surfaces: README,
   **docs/guide/*.md** (the user-facing guide pages the 2026-07-16
   restructure moved the README's deep material into — configuration,
   retrieval, dreaming, episodes, memory-model, benchmarks; they carry the
   same drift-prone claims the README used to),
   CONTRIBUTING, SECURITY, evals/README, examples/ (CLAUDE.memory.md is
   injected into user CLAUDE.mds — its tool surface must match exactly),
   docs/runbooks, ops/.env.example comments. The README is the PyPI
   description, so its fixes only reach PyPI at the next version.

1. **Version cut touches six files together**: the CHANGELOG (`## [N.N.N]`
   header over `[Unreleased]` — one fragile line; the tag↔section guard test
   exists because an adjacent edit once deleted it silently), `pyproject.toml`,
   the compose daemon image tag, **both** version fields in `server.json`,
   `plugin/.claude-plugin/plugin.json` (pinned to pyproject by
   `tests/test_plugin_packaging.py`; the plugin marketplace serves from
   this repo, so bumping it is also what ships plugin updates), and
   `docs/atlas/atlas.json` `meta` (pinned by `tests/test_atlas_currency.py`;
   re-verify the map's claims, don't just renumber it — update
   `meta.verified` to the date you actually checked).
   Tag `vN.N.N` at the exact commit the artifacts build from.
2. **Build + inspect before upload**: `python -m build`, `twine check dist/*`,
   then open the wheel — Console static assets present (33 files under
   `web/static/`), no stray top-level dirs, the `mcp-name` marker in METADATA,
   no identifiers (grep the METADATA for the guard list).
3. **PyPI**: publishing the GitHub release triggers
   `.github/workflows/release.yml` (Trusted Publishing — OIDC, no token):
   it guards tag == pyproject version, builds, twine-checks, then waits for
   the user's one-click approval on the `pypi` environment. Manual
   `twine upload dist/*` remains the fallback. PyPI never accepts a
   same-version re-upload — metadata-only fixes are a `.postN`.
4. **MCP registry — automated.** The `registry` job in `release.yml` runs
   after the PyPI job and authenticates with `mcp-publisher login
   github-oidc` (same trust model as Trusted Publishing: a short-lived token
   minted per run, nothing stored, nothing to expire between cuts). It
   guards **both** `server.json` version fields against the tag, waits for
   PyPI to actually serve the new version, checks the marker survived into
   the published description, publishes, then confirms the registry serves
   it as latest. Publishing the GitHub release is now the single action that
   lands all four surfaces.
   *Manual fallback* (`mcp-publisher login github` then `publish`, from the
   repo root) if the job is ever broken — note the binary is often not on
   `PATH`; resolve it with `Get-Command mcp-publisher` rather than assuming.
   Either way: the README marker must read exactly
   `mcp-name: io.github.Pseudogiant-xr/pseudolife-mcp` — the namespace is
   matched **case-sensitively** against the GitHub username (capital P), and
   validation reads the **latest** PyPI release's description. The registry
   `description` field caps at 100 chars. Verify:
   `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=pseudolife"`.
