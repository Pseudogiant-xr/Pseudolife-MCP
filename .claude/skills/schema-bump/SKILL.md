---
name: schema-bump
description: Use for any change to pseudolife_memory/storage/schema.py, and for every schema version bump (SCHEMA_META_VERSION). Covers the eight Python-side steps a bump touches, the Rust daemon's embedded schema plan, the schema startup golden, and the Rust parity pins and harness goldens a bump moves.
---

# Schema change and schema bump checklist

A schema bump touches several files that each have a guard test, and the
Rust port pins the schema in more places still. Missing one fails CI in a
lane the master ruleset does not require, so it is easy to merge broken.
The v30 bump found steps 7 and 8 the hard way; the v56 bump missed every
Rust step below and four Parity jobs failed, and its CLI harness goldens
failed a second round after the first re-record.

## A. Every schema bump (Python side, all in one PR)

1. **The version.** `SCHEMA_META_VERSION` in
   `pseudolife_memory/storage/schema.py`.
2. **The docs.** The README capabilities table, and in
   `docs/guide/configuration.md` the DSN row and the version-history table.
   `tests/test_release_ux.py` pins both, the history table including gap
   detection.
3. **The version pin.** The single `CURRENT_SCHEMA` literal in
   `tests/test_schema_version.py`. No per-version test file and no `>=`
   relaxation pass: the ladder is gap-checked by `test_release_ux.py` and
   `test_atlas_currency.py`.
4. **The behaviour's test.** Whatever the bump adds gets a test beside its
   consumer, or a row in `tests/test_schema_ddl_shape.py` if it is pure DDL
   shape. Never a new `test_schema_vNN.py`.
5. **The changelog.** A `vNN` mention in the change's `changelog.d/`
   fragment (`test_release_ux.py` reads CHANGELOG.md plus pending
   fragments).
6. **The atlas.** `docs/atlas/atlas.json` `meta.schema`, pinned by
   `tests/test_atlas_currency.py`. Re-verify the affected storage cards;
   don't just renumber.
7. **The embedding-migration pins.** The two `assert meta[0] == NN`
   literals in `tests/test_migrate_embeddings.py`.
8. **The generated docs.** `python ops/gen_llms_txt.py` after any doc edit
   (`tests/test_llms_txt.py` pins the generated `llms-full.txt`).

## B. Any change to `schema.py`, bump or not

The Rust plan hashes the whole file, so even a comment change needs this.

9. **Regenerate the Rust daemon's embedded schema plan:**
   `python rust/daemon/harness/gen_schema_sql.py`, then the same command
   with `--check` must pass. The changed `schema.sql`, `schema_plan.json`
   and `schema_meta.rs` under `rust/daemon/src/storage/` ship in the same PR.
10. **When the DDL or migrations change, re-record the schema startup
    golden** `rust/daemon/harness/goldens/schema-startup.json`:
    `python rust/daemon/harness/schema_ci.py --record-goldens --out
    <scratch>/schema-record.json`. It needs the bench Postgres, a Rust
    toolchain, and the commits listed in
    `rust/daemon/harness/schema_history.json`; `--no-build --rust-test-bin
    <exe>` reuses a built test binary. Inspect the golden's diff, then run
    it again without `--record-goldens` to check.

## C. A schema bump also moves the Rust parity pin (`rust/PARITY.md`)

11. **The shim's transfer schema literals** in
    `rust/shim/src/cli/transfer/{export,import}.rs` and `transfer.rs`
    (`tests/test_schema_version.py` pins them).
12. **Every harness golden that digests the catalog or dumps a bank**,
    re-recorded on both OSes:
    - dream cursors: `rust/daemon/harness/dream_cursors.py live --record`;
    - the daemon `run.py` golden rows, the graph store and the principals;
    - the CLI harness rows whose goldens carry a bank dump or an export
      (`rust/cli_harness/goldens/<row>.{linux,windows}.json`: transfer,
      invite and maintainer at least):
      `python rust/cli_harness --row <row> --record --candidate <shim>`.

## D. Before merge

13. The master ruleset requires only the Python `test` job, so the Rust
    workflow's schema and Parity checks must be green before merge
    regardless.
14. A schema/DDL/migration change requires a local full suite (CLAUDE.md,
    "Running tests").
