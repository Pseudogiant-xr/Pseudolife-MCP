# W2-F forward review checklist

The paused coordination-board port now contains master `4b34c81e` through a
merge, preserving the original `decf25dc` history. The source branch remains
unchanged; this work is local on `rust-cf/w2f-core-fwd` and is not published.

## Conflict resolutions

All 26 conflicted paths are listed below. "Master" means the complete current
master version was retained, rather than the earlier W1-A copy in W2-F.

| Conflicted file | Resolution and reason |
|---|---|
| `rust/.gitattributes` | Master: retain newline rules for all current embedded assets. |
| `rust/Cargo.lock` | Start with master, let Cargo regenerate dependency resolution after adding board dependencies; no manual lockfile merge. |
| `rust/daemon/Cargo.toml` | Master plus W2-F's `uuid`, `hmac`, Unicode crates and `getrandom`; retain graph harness, platform dependencies and shared version pins. |
| `rust/daemon/README.md` | Master plus a link to the board harness; retain current startup and model instructions. |
| `rust/daemon/divergences.md` | Master; remove the now-implemented board and board-hook 501 rows, retaining other slices' limitations. W2-F's own divergences remain in `spec.md`. |
| `rust/daemon/harness/daemons.py` | Master: preserve static fixtures, refusal-port handling and process isolation. |
| `rust/daemon/harness/dbstate.py` | Master: preserve catalog/type coverage and exact comparison of pre-existing clock values. |
| `rust/daemon/harness/gen_schema_sql.py` | Master: retain schema generation and migration-plan support. |
| `rust/daemon/harness/pgdisposable.py` | Master: retain test-login, production-bank refusal, per-slice naming and bounded drop retries. The board runner explicitly selects `w2f` before importing it. |
| `rust/daemon/harness/record_routes.py` | Master: retain current route and embedded-checkin recording. |
| `rust/daemon/harness/run.py` | Master: preserve the shared foundation corpus and current startup/static/embedding cases. |
| `rust/daemon/spec.md` | Master foundation plus the complete W2-F coordination-board contract and its declared divergences. |
| `rust/daemon/src/auth.rs` | Master: preserve Python whitespace handling and current authentication behavior. |
| `rust/daemon/src/bank.rs` | Master: preserve complete startup hydration and dimension guards. |
| `rust/daemon/src/config.rs` | Master plus W2-F's coordination fields, defaults and parsing; retain all unrelated startup, embedding and configuration behavior. |
| `rust/daemon/src/embed.rs` | Master: preserve current model loading, embedding and backend checks. |
| `rust/daemon/src/health.rs` | Master: preserve current health fields and parsing. |
| `rust/daemon/src/http.rs` | Master gate, JSON writer, browser policy and static dispatch plus W2-F's board field, coordination handlers, board hooks, Console coordination view and daemon notice. |
| `rust/daemon/src/main.rs` | Master module list and startup plus `mod board` and board construction; retain warmup, reaper and complete startup wiring. |
| `rust/daemon/src/mutants.rs` | Master: retain current harness capability and static fixture controls; board mutants use the existing `active` API. |
| `rust/daemon/src/search.rs` | Master: preserve current read-path behavior. |
| `rust/daemon/src/service.rs` | Master plus W2-F's six-line storage-only tier method; preserve spawned initialization, complete required loaders and late-clock retry. |
| `rust/daemon/src/static_files.rs` | Master: preserve current static containment and platform behavior. |
| `rust/daemon/src/storage/mod.rs` | Master: preserve leased-writer lifecycle and seam use. |
| `rust/daemon/src/storage/schema.rs` | Master: preserve generated schema plans, migration/startup guards and seam use. |
| `rust/daemon/src/storage/tests.rs` | Master: retain current storage tests. |

The board modules, float vectors and board harness all come from W2-F. Shared
workspace/module additions coexist; `txn.rs`, startup modules, CI shards and
the producer census are unchanged from master. Formatting applies the current
Rust formatter to the inherited board code. Clippy cleanup only simplifies the
fan-out Option match and redundant conversions/closures, moves a test module
after its helpers, and annotates existing contract-shaped argument lists.

## Writer seam adoption

- `board::dispatch::Board::tick` calls `board::hlc::stored_high_water` with
  `Storage::client()`, the leased writer.
- `board::hlc::stored_high_water` now enters `txn::with_client` before the
  existing read group. Its private inner function runs the three highest-HLC
  queries on `facts`, `world_facts` and `lessons`, then reads
  `coordination_hlc_highwater` from `meta`, on that borrowed client.
- `Service::storage_tier` delegates storage opening to master's existing
  `ensure_storage`; schema, metadata and writer-epoch operations retain
  master's seam implementation. `txn.rs` is unchanged.

## Decisions for Opus

The brief's broad "every DB access" sentence is read together with its explicit
leased-client requirement and the existing dedicated-connection contract. The
delegate confirmed that the board's separate connection must not share the
writer's process-global `OPEN` marker: a failure on one client could otherwise
cause recovery on another client. This is an explicit exception to routing
every board access through the writer helper, for review before publication.

The following dedicated-client transactions are retained with their original
boundaries and `tokio_postgres::Transaction` rollback-on-drop behavior:

- `store`: `context`, `register`, `update`, `subagent_started`,
  `subagent_stopped`, `roster`'s settlement, `attach`, `heartbeat`, `detach`,
  `woke`.
- `mail`: `send`, `receive`'s first-read write, `ack`, `mark_attempt`,
  `history`, `prune`.
- `lease`: `acquire_lease`, `release_lease`, `list_leases`' settlement.
- `console`: `console_snapshot` retains its read-only repeatable-read snapshot.

Dedicated-client autocommit reads/settings are also retained: `db::Conn::connect`
sets session options; `Store::list_agents` authenticates before its roster call;
`roster_read`, `check_attachment` and `park_gate`, and the read portions of `receive` and
`list_leases`, continue on the board connection under its existing mutex.
Generic SQL helpers run within those entry points or their existing transactions.
`Board::with_store`, dispatch, Console and notice entry points keep the same
dedicated connection. No local recovery helper or transaction redesign was added.

Other choices and limitations:

- The board runner selects its own `w2f` disposable namespace; a namespace
  preflight was watched fail before this change, then pass, and a regression
  case was added to `board/test_norm.py`.
- Pairing remains the original branch's deferred stub; this forward adds no
  pairing write or new board feature.
- The shared foundation harness and CI inventory retain master's expectations;
  they were not rewritten to mark board cutover complete. This handoff runs the
  board's own harness, not all other slices' harnesses or Python full suites.
- Original golden replay pacing remains an open issue; no normalizer, sleep,
  wake policy or golden expectation was changed to hide it.
- The standalone board clock remains the branch's existing clock, rather than
  being redesigned to use another slice's resident clock.

## Validation (2026-10-10)

Code candidate: merge `86393d17577c57255c5e5730719e6837fbbb48c1`.
Logs and JSON artifacts are kept in the private task evidence directory; raw
logs are not committed because they contain machine-local paths.

| Environment | Command / artifact | Exit | Result |
|---|---|---:|---|
| Windows | `cargo fmt --manifest-path rust/Cargo.toml --all --check` / `S3-fmt.log` | 0 | Formatting matches. |
| Windows | `cargo clippy --manifest-path rust/Cargo.toml -p pseudolife-daemon --all-targets -j 4 -- -D warnings` / `S3-clippy-green.log` | 0 | No warnings. |
| Windows | `cargo test --manifest-path rust/Cargo.toml -p pseudolife-daemon --all-targets -j 4` / `S3-cargo-test.log` | 0 | 107 tests reported passing. Optional storage/clock fixture tests return early without their explicit DSN variables; this does not claim live PG coverage on Windows. |
| Windows | `python -m pytest rust/daemon/harness/board/test_norm.py tests/test_changelog_fragments.py -q` / `S3-python-targeted.log` | 0 | 39 tests passed. |
| Assigned W2-F Linux lane | `cargo build --manifest-path rust/Cargo.toml -p pseudolife-daemon --features mutants -j 4` / `S3-build.log` | 0 | Built the exact candidate in a separate bundle checkout and target directory. |
| Assigned W2-F Linux lane | `python -m board.run live --only identity --rust-bin <binary> --out S3-identity.json` | 0 | 84 steps, no step/state/chain differences. |
| Assigned W2-F Linux lane | `python -m board.run live --rust-bin <binary> --out S3-live.json` | 0 | 263 steps, 11 scenarios, zero step/state/chain differences, including both mixed-chain directions. |
| Assigned W2-F Linux lane | `python -m board.run mutants --only identity mail wake leases --rust-bin <binary> --out S3-mutants.json` | 0 | All eight mutations caught, no survivors; these four scenarios exercise their activation sites. The full 263-step baseline ran first. |
| Assigned W2-F Linux lane | `python -m board.run golden --rust-bin <binary> --out S3-golden.json` / `S3-golden.log` | 1 | Aborted before completion: state refresh queried `coordination_events` while the table was absent. No JSON result was written. A startup-readiness race is inferred, not fully diagnosed; this harness issue is left for Opus. |
| Assigned W2-F Linux lane | `python -m board.run golden --only mail --rust-bin <binary> --out S3-golden-mail.json` | 1 | Known pacing failure still reproduces: 62 steps, 4 step differences, 180 downstream state differences, 0 chain problems. Pre-pause goldens were copied unchanged, not re-recorded. |
| Windows | Publication check over `origin/master..rust-cf/w2f-core-fwd` / `S3-publication-final.log` | 0 | Committed handoff range and commit-message scan; no findings. |
| Windows | `python rust/producer_census.py --check` / `S3-producer-census.log` | 0 | Existing generated inventory is current; no inventory or producer-shape change. |

Earlier failures are recorded separately: `S3-red-fmt.log` (exit 1 because the
initial workspace formatter invocation lacked `--all`),
`S3-seam-check.log` (exit 101 on the subsequently discarded dedicated-client
conversion), `S3-red-prefix.log` (exit 1 before namespace selection), and the
first Linux identity preflight (exit 1 because the expected test-login file was
absent). The Linux run uses a private login fixture derived from the lane's
existing test configuration and explicitly selects test PostgreSQL port 5434.
No production bank, full suite, GPU run, deployment or public push is involved.

The mutant artifact records the following observed differences:

| Mutation | Step differences | State differences | Chain problems |
|---|---:|---:|---:|
| `board-chain-skip-seq` | 3 | 1418 | 4 |
| `board-clock-behind` | 99 | 3606 | 0 |
| `board-lease-never-expires` | 2 | 8 | 0 |
| `board-canonical-unsorted` | 0 | 0 | 77 |
| `board-urgent-cap-off` | 4 | 61 | 0 |
| `board-no-read-event` | 3 | 270 | 0 |
| `board-receipt-path-live` | 19 | 0 | 0 |
| `board-park-default-ttl` | 6 | 440 | 0 |
