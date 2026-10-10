# W2-G forward review checklist

The paused MCP surface branch predates the shared writer seam and complete
startup hydration. This local merge-forward preserves its history and MCP
implementation while retaining current master's infrastructure.

## Inputs and boundary

- Paused branch: `rust-cf/w2g`, `1b2da20da`; left untouched.
- Forward branch: `rust-cf/w2g-fwd`.
- Merged master: `4b34c81ef053ae2966ac8605d2680c36a182e6cb`.
- No public push or PR. This checklist is for the paused author's review.
- Tokenless `/mcp` and `/mcp/*` remain 501, including redirects and other
  methods. Bearer-configured installs pass through the existing authentication
  gate before the MCP transport. Bound-identity validation remains deferred.

## Conflicts and resolutions

| Conflicted file | Resolution and reason |
|---|---|
| `rust/.gitattributes` | Master: retain every current embedded-asset newline pin. |
| `rust/Cargo.lock` | Start with master and regenerate through Cargo after restoring daemon dependencies; no hand merge. |
| `rust/daemon/Cargo.toml` | Master plus W2-G's `uuid` and `futures` dependencies; keep graph harness and platform dependencies. |
| `rust/daemon/divergences.md` | Master infrastructure declarations plus W2-G MCP section; remove the authenticated MCP 501 row, retain the tokenless cutover blocker and bound-identity deferral; remove stale body-limit declaration (transport enforces 4 MiB). |
| `rust/daemon/harness/daemons.py` | Master: preserve static/bind fixtures and environment/port behavior. W2-G uses its unchanged public helper signatures. |
| `rust/daemon/harness/gen_schema_sql.py` | Master: preserve schema generation and history metadata. |
| `rust/daemon/harness/pgdisposable.py` | Master: preserve shared foundation disposable-bank safeguards. W2-G has its own `mcp_run.py` namespace. |
| `rust/daemon/harness/record_routes.py` | Master: retain current route and check-in recording. |
| `rust/daemon/harness/run.py` | Master: retain foundation, startup, embedding and static acceptance cases. |
| `rust/daemon/src/bank.rs` | Master: retain complete hydration, dimensions and writer seam. |
| `rust/daemon/src/config.rs` | Master: retain current configuration parser and dump fixtures. |
| `rust/daemon/src/embed.rs` | Master: retain current embedding artifact/runtime integration. |
| `rust/daemon/src/health.rs` | Master: retain current health surface. |
| `rust/daemon/src/http.rs` | Master gates, serialization and dispatch; add W2-G `McpState` and authenticated MCP handoff, with tokenless 501 guard. |
| `rust/daemon/src/main.rs` | Master startup/module list plus `mod mcp`; retain startup and transaction modules. |
| `rust/daemon/src/mutants.rs` | Master: its generic named-mutant helper already serves W2-G, while preserving the loopback fixture capability. |
| `rust/daemon/src/service.rs` | Master: retain all-or-nothing startup and detached initialization; no rollback to the paused partial hydration. |
| `rust/daemon/src/static_files.rs` | Master: retain static response and traversal fixes. |
| `rust/daemon/src/storage/mod.rs` | Master: retain current storage and writer seam. |
| `rust/daemon/src/storage/schema.rs` | Master: retain schema/version handling. |
| `rust/daemon/src/storage/tests.rs` | Master: retain current foundation tests. |

W2-G's `src/mcp/*`, `harness/mcp_*`, catalogue, tiers and MCP goldens remain
from the paused branch. Rust formatting is applied. The difflib loop uses
equivalent enumeration to satisfy clippy; the agreed dispatch interface keeps
documented unused-code and large-enum allowances until its body slices land.
The MCP spec and changelog now explicitly describe bearer-only mounting.

## Writer seam and principal decisions for Opus

- No new seam adoption sites: MCP transport, catalogue, tier overrides and
  dispatch own no PostgreSQL client and issue no SQL. Shared storage, bank and
  startup code retain master's `txn::run` / `txn::with_client` adoption.
- `principals.rs` is byte-identical to the merged master. Its dedicated
  `Refresher::read` connection setup and `load_rows` reads keep their own client;
  they do not share `txn`'s global OPEN marker. Call sites:
  `Refresher::read` calls `load_rows`, and `spawn_refresher` runs `Refresher::run`.
- Stored-tier lookup still uses `auth::PrincipalStore::tier_of` through
  `McpState::stored_tier_for`; only the request's named bearer takes its stored
  tier. A default bearer asserting another principal's writer header does not.
- PR #688 was not merged at the master input above. Reconcile its store changes
  if it lands before publication; this forward does not anticipate its choices.
- Keep the tokenless guard until the standing Host/Origin policy decision is
  cleared. The inherited tokenless differential corpus describes the future
  transport policy and is expected to differ from the deliberate 501 boundary.
- Keep existing catalogue and MCP goldens unchanged; any Python-oracle drift
  found below belongs to the paused author's promotion decision.
- No tool bodies are newly registered. Generic binding outside `memory_toolset`,
  bound identity and stateless-era support retain their declared deferrals.

## Validation (2026-10-10)

- Windows watched RED: `cargo test -p pseudolife-daemon -j 2
  tokenless_mcp_remains_unserved`, exit 101: GET `/mcp` returned 406 instead of
  501 before restoring the guard.
- Windows `cargo fmt --all --check`, exit 0.
- Windows `cargo clippy -p pseudolife-daemon --all-targets -j 2 -- -D warnings`,
  exit 0; initial clippy exposed inherited dispatch/difflib warnings, addressed
  mechanically as listed above.
- Windows `cargo test -p pseudolife-daemon -j 2`, exit 0: 115 passed. Includes
  MCP goldens and tokenless guard; optional real-PG tests returned early because
  their DSNs were unset. This is not a real-PG validation claim.
- Windows `python -m pytest tests/test_rust_mcp_catalogue.py -q`, exit 0:
  2 passed; current Python catalogue values and tiers match recorded assets.
- Linux build, crate tests, live differential, side configuration, mutation
  control and publication scan: pending at this commit; results follow below.

No deployment, full suite, GPU launch, public push or PR was performed.
