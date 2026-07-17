# Pseudolife-MCP — project conventions

Conventions that aren't derivable from a quick read of the code. Follow them
exactly; they exist because each one was violated at least once.

## Shipping checklist (any change that lands on master)

1. **CHANGELOG.md entry under `[Unreleased]`** — every behavior, schema, or
   perf change gets one, in the existing dated-subsection style. Docs-only and
   test-only changes are exempt.
2. **Schema bumps** touch four places together: `SCHEMA_META_VERSION` in
   `pseudolife_memory/storage/schema.py`, the doc mentions (README
   capabilities table + the DSN row and version-history table in
   `docs/guide/configuration.md` — pinned by `tests/test_release_ux.py`),
   the version-pin tests (`test_schema_v13.py`, `test_schema_v16.py`,
   `test_temporal_stamp.py`, plus a new `test_schema_vNN.py` for the
   addition itself), and a CHANGELOG mention of `vNN` (pinned by
   `test_release_ux.py`).
3. **Full suite before commit** — `HF_HUB_OFFLINE=1 python -m pytest tests/`
   with the bench Postgres up (127.0.0.1:5433); PG-backed tests skip silently
   without it, which is not a pass.
4. **Deploy only via `ops/update.ps1`** (backup → rollback tag → daemon-only
   `--no-deps` rebuild → health). Never `docker compose down -v` — the bank
   volumes are external precisely so that this is survivable, but don't test it.
5. **After deploy, verify live**, not just `/health`: exercise the changed path
   through the daemon (an MCP call, a psql check of new DDL).

## Derived state / caches / indexes

When adding any derived structure over mutable state (an index over band
entries, a cached view of the graph, a memoized score):

- **Enumerate every mutation path first**, including the ones that bypass the
  normal write API: `hydrate_cms` / `load()` / legacy migration append to
  `band.entries` directly and never call `store()`. Grep for the state being
  mutated, not for the API you expect callers to use.
- **State the real workload's read/write interleave and check the maintenance
  policy preserves the win under it.** The daemon's steady state is
  store/search alternation: an invalidate-on-every-write policy rebuilds on
  every read and silently degrades to the cost you were optimizing away.
  Extend-in-place for additions; rebuild only on removals/replacement.
- **List what the replaced code provided implicitly** — iteration order
  (tie-break determinism), containment semantics (`bands=` filters on the band
  that holds the entry, not `entry.bank`, which goes stale across preset
  changes), live-object reads (supersession flags are read at query time).
  Preserve each one or change it consciously and say so in the commit.

## Review discipline

- Perf/cache/index changes get an independent review pass before commit
  (`/code-review` medium, or a reviewer subagent) — the 2026-07-12 slot-index
  audit found three of these classes post-deploy; the pass is cheaper.
- TDD with a watched RED per the superpowers skill; for invalidation contracts,
  spot-check that each hook is load-bearing by disabling it and confirming the
  test goes red (a hook that never fires red is decoration, and worth saying so).
- Eval- or retrieval-affecting changes run `evals/regression_gate.ps1`
  before commit (pinned replicated slice vs committed baseline; exit 1 =
  regression). Extraction/dream-path changes re-run the ladder instead —
  the gate deliberately does not cover them.

## Release / publish procedure (four public surfaces)

GitHub releases, PyPI, the MCP registry, and the Claude Code plugin
marketplace (`.claude-plugin/marketplace.json` + `plugin/` — served straight
from master, no publish step; users pull via `/plugin marketplace update`)
all serve from this repo; a release touches them in this order (first done
2026-07-16, v0.8.0).

0. **Docs currency pass before the cut** — the guard tests pin numbers
   (schema, identifiers), but *framing* drifts silently: the 2026-07-16
   pass found 15 stale claims the guards can't see. Re-verify the
   drift-prone claim classes against code before any release:
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

1. **Version cut touches five files together**: the CHANGELOG (`## [N.N.N]`
   header over `[Unreleased]` — one fragile line; the tag↔section guard test
   exists because an adjacent edit once deleted it silently), `pyproject.toml`,
   the compose daemon image tag, **both** version fields in `server.json`,
   and `plugin/.claude-plugin/plugin.json` (pinned to pyproject by
   `tests/test_plugin_packaging.py`; the plugin marketplace serves from
   this repo, so bumping it is also what ships plugin updates).
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
4. **MCP registry** (`mcp-publisher login github` is the user's; `publish` is
   scriptable): the README marker must read exactly
   `mcp-name: io.github.Pseudogiant-xr/pseudolife-mcp` — the namespace is
   matched **case-sensitively** against the GitHub username (capital P), and
   validation reads the **latest** PyPI release's description. The registry
   `description` field caps at 100 chars. Verify:
   `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=pseudolife"`.

## Repo hygiene — no PII, ever (public repo)

Anything pushed is public forever: GitHub keeps merged-PR commits reachable
via `refs/pull/*`, which owners cannot purge (Support ticket only) — one
leaked email already cost a full history rewrite plus a fresh-repo publish.

- **Never commit PII or machine identifiers**: emails, OS usernames
  (`C:\Users\<real name>`), hostnames, LAN IPs/subnets, tokens/keys. Docs and
  tests use placeholders (`<user>`, `example.com`) or the synthetic `10.0.0.x`
  examples already in the tree.
- **Extend the guard, don't just scrub**: a removal without a test regresses
  (2026-07-12 lesson). Any newly-spotted identifier class gets added to
  `tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`
  with a watched RED before the scrub.
- **Commit identity stays the GitHub noreply address**
  (`Pseudogiant-xr@users.noreply.github.com`); tee'd script output
  (`deploy-*.log` etc.) stays out of the tree — it embeds absolute home paths.
- **Commit METADATA is a leak channel the guard test can't see**: GitHub
  web-UI edits stamp the account's real email unless Settings → Emails →
  "Keep my email addresses private" is ON (verified on, 2026-07-16 — it was
  off, and one web edit leaked; inspect any unexpected remote commit with
  `git show --format=%ae` before building on it).
- **If a real secret ever lands in a pushed commit: rotate it first.** A
  rewrite is tidiness, not remediation.

## Memory (Pseudolife MCP tools)

Log `memory_outcome` at task end — success/failure/correction signals are the
only feeder for the lessons surfaced at session start. Deploys and eval results
get a `memory_store` with source `pseudolife-mcp` (status chatter →
`source="status"`).
