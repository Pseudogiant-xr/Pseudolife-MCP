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
| `rust/daemon/harness/run.py` | Master plus the W2-G authenticated MCP case: compare the served transport instead of requiring a 501 stub; retain tokenless 501 declarations and every other foundation/startup/embedding/static case. |
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
- Windows foundation harness regression: watched RED (1 failed, 1 passed,
  exit 1) on the stale authenticated 501 declaration; after the single-case
  correction, `python -m pytest rust/daemon/harness/test_mcp_mount.py -q`,
  exit 0: 2 passed. An incorrect 501 authenticated answer and a non-501
  tokenless answer both remain load-bearing failures.
- Linux at `74ee4a39`: build with `--features mutants -j 3`, fmt check,
  clippy with warnings denied, and crate tests all exit 0; 120 tests passed.
  Optional real-PG unit tests were not configured in this first crate run.
- Linux `mcp_run.py <binary> <out>` at `74ee4a39`, exit 0: 261 responses,
  16 cases, zero diffs, all 37 unserved tool stubs checked. The comparison
  includes exact tier-list bytes, tiers, stored-principal mappings, writer
  headers, transport/session behavior and `memory_toolset`.
- Linux `mcp_run.py ... --config2`, exit 0: 10 responses, zero diffs.
- Linux `mcp_run.py ... --tokenless`, exit 1: 30 oracle responses and
  8 reported diffs under the deliberate 501 boundary. The inherited future
  policy corpus includes requests requiring a live MCP session, so its Rust
  rebinding sequence stops early when initialize receives 501. This is a
  declared limitation, not a passing parity claim.
- `mcp-bare-host` is unreachable through the production tokenless mount;
  its mutation control remains deferred with the tokenless policy. Four
  authenticated controls passed: `mcp-tier-flip` (36 diffs),
  `mcp-toolset-floor` (30), `mcp-sse-no-session` (233), `mcp-no-hint` (3).
  Each wrapper exited 0 only after the comparator exited 1 with actual diffs;
  none was counted as caught because of a harness crash.
- Dedicated Linux PostgreSQL unit runs at `74ee4a39`: `cargo test
  -p pseudolife-daemon -j 3 storage::tests -- --nocapture --test-threads=1`,
  exit 0 (11 passed); the same command with `principals::tests`, exit 0
  (3 passed). `PL_W1A_TEST_DSN` and `PL_W1A_EMPTY_DSN` named separate
  disposable W2-G databases on the assigned test PostgreSQL lane, so the
  table-loading and empty-schema paths actually ran.
- Linux foundation checks at `39d3eb6a`: pytest launch exited 1 because the
  isolated environment has no pytest module. Direct execution of both
  `test_mcp_mount.py` functions exited 0 (2 passed), with no environment
  installation or skipped assertions. Windows pytest had already passed
  both tests on the same source.
- PR #688 was still open at the final master check; `origin/master` remained
  `4b34c81e`. The original `rust-cf/w2g` remained `1b2da20da`.
- All owned Linux build/harness/control processes exited, both worktrees
  were clean, and disposable W2-G databases were dropped by their runners.
- Publication scans of `origin/master..rust-cf/w2g-fwd` at `74ee4a39` and
  `39d3eb6a`, exit 0.
  Catalogue and goldens were not re-recorded; no oracle drift was observed
  in authenticated MCP or the alternate configuration.

## Evidence and handoff

The daemon source and Cargo inputs are unchanged from tested `74ee4a39`;
`39d3eb6a` changes only the foundation case, its test and this checklist.
The following checklist update changes documentation only.

Private execution logs and JSON reports accompany the local handoff; they
are not committed because they contain machine paths. The final handoff
names their paths and the tested code commit. Review by the paused author
is pending; this branch is a review candidate, not a publication decision.

No deployment, full suite, GPU launch, public push or PR was performed.

## W2P preparation, 2026-10-12

- BASE: `aaa652c6f570d02d9b2985acf1b3da5e1c45e92d`.
- Local branch: `w2p/g`; no publication.
- Conflicts: `../../rust/Cargo.lock`, `src/http.rs`, `src/main.rs`.

Master lock is retained for Cargo regeneration. Module declarations retain MCP and maintenance. Authenticated MCP keeps principal dispatch and tokenless 501, using the shared counted HTTP body reader.

NEEDS OPUS: shared bounded refusal cleanup replaces the older MCP detached body drain; review transport deadline policy at landing. Bound identity and tool-body integration remain deferred.

Validation and independent Codex high review are recorded in the private W2P report; initial failures and final reruns are listed separately. Windows evidence is cross compilation, not runtime. Frozen oracle goldens are not re-recorded.

### W2P completed evidence

At `9b29830f5`, Linux fmt, all-targets clippy with warnings denied, tests and mutant build exit 0 (188 tests, including the distilled committed MCP goldens). Daemon and stdio Windows GNU check and clippy exit 0; no Windows runtime claim. Independent Codex high review found that the initial shared-body adaptation accidentally deleted MCP declarations. A compile RED (62 errors) was observed, those declarations were restored while keeping the counted reader, and the final Cargo checks pass.

Authenticated live MCP: 261 responses, 16 cases, 4 differences, all from master's bounded refusal cleanup adding `Connection: close`; 37 unserved tools are checked. Alternate config: 10 responses, 0 differences. Tokenless future-policy corpus: 30 responses, 8 differences under the deliberately retained 501 guard. No golden or comparator was changed. NEEDS OPUS: shared HTTP refusal/connection policy at landing, plus existing tokenless Host/Origin, bound-identity and tool-body integration deferrals.

Four authenticated mutation controls produced 40, 34, 236 and 7 total differences respectively. Because the ordinary live baseline has four differences, the private verification also checks that each control has changed or additional differences beyond that baseline: 36, 30, 232 and 3 respectively. The tokenless bare-host control remains unreachable through the 501 mount and is deferred, not counted as caught.

The MCP runner now uses master disposable-name ownership and default-template safeguards. Owned fixture processes and banks were cleaned by the runners. Private W2P `g/` contains Cargo evidence, live/alternate/tokenless results, all control results and the review. Conflict detail: Cargo.lock keeps master resolution and adds the union through Cargo; main.rs retains MCP and maintenance modules; http.rs retains named-principal MCP dispatch, tokenless 501 and shared counted body ownership.
