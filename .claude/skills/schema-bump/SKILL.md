---
name: schema-bump
description: Use for changes to the Rust daemon's schema.sql or schema_plan.json and for every schema version bump. Covers native schema inputs, generated metadata, startup and harness goldens, and release and transfer guards.
---

# Schema change and schema bump checklist

A schema bump changes the native schema inputs and the release and transfer surfaces that advertise the version. Land them together with the affected golden updates.

## A. Every schema bump (all in one PR)

1. **The version.** Set `schema_version` and the final `meta.schema_version` stamp parameter in `rust/daemon/src/storage/schema_plan.json`. Set the matching version header in `rust/daemon/src/storage/schema.sql`.
2. **The docs.** The README capabilities table, and in
   `docs/guide/configuration.md` the DSN row and the version-history table.
   `tests/test_release_ux.py` pins both, the history table including gap
   detection.
3. **The version pin.** The single `CURRENT_SCHEMA` literal in
   `tests/test_schema_version.py`. No per-version test file and no `>=`
   relaxation pass: the ladder is gap-checked by `test_release_ux.py` and
   `test_atlas_currency.py`.
4. **The behaviour's test.** Test the change beside its consumer, or extend the native schema startup checks for a DDL-only change. Keep tests grouped by behaviour.
5. **The changelog.** A `vNN` mention in the change's `changelog.d/`
   fragment (`test_release_ux.py` reads CHANGELOG.md plus pending
   fragments).
6. **The atlas.** `docs/atlas/atlas.json` `meta.schema`, pinned by
   `tests/test_atlas_currency.py`. Re-verify the affected storage cards;
   don't just renumber.
7. **The frozen migration pins.** The v25 legacy migration uses the frozen package and keeps that package's version in `tests/test_migrate_embeddings.py`.
8. **The generated docs.** `python ops/gen_llms_txt.py` after any doc edit
   (`tests/test_llms_txt.py` pins the generated `llms-full.txt`).

## B. Any native DDL or migration change

9. **Edit the native inputs and derive the metadata.** Under `rust/daemon/src/storage/`, edit `schema.sql` for base DDL and `schema_plan.json` for ordered migration steps and schema metadata. Preserve the startup transaction and keep the version and dimension refusals before DDL. Run `python rust/daemon/harness/gen_schema_sql.py` to derive `schema_meta.rs`, then run the same command with `--check` to verify the native inputs and derived metadata. Ship the changed files together.
10. **When the DDL or migrations change, re-record the schema startup
    golden** `rust/daemon/harness/goldens/schema-startup.json`, and, when
    what a start writes or hydrates changes, `startup-hydration.json` and
    `clock-retry.json` beside it. Since the Python freeze these are Rust
    regression snapshots, recorded from the Rust candidate:
    `python rust/daemon/harness/schema_ci.py --record-candidate --out
    <scratch>/schema.json` (and `startup_ci.py --record-candidate` for the
    other two). That is a dry run: it prints each changed case's difference
    (the committed golden labelled `golden`) and exits 1 if anything would
    change; review it, then add `--write`. A case whose run fails the
    harness's own checks is refused and nothing is written. It needs the
    bench Postgres, a Rust toolchain, and the commits listed in
    `rust/daemon/harness/schema_history.json`; `--no-build --rust-test-bin
    <exe>` reuses a built test binary. One golden serves both operating
    systems, so then run `schema_ci.py` and `startup_ci.py` without a
    recording flag (what CI runs: Rust only, with the mutant controls) on
    this OS and the other, and name the cases and the reason in the pull
    request (`rust/daemon/harness/candidate_record.py` has the rules).
    `--record-goldens` is retired and refuses, because it recorded the
    frozen Python service; `--record-candidate` records from Rust, as above.

## C. A schema bump also moves the Rust parity pin (the register, `rust/parity/`)

11. **The shim's export schema literal** in `rust/shim/src/cli/transfer/export.rs` (`tests/test_schema_version.py` pins it). Import embeds the daemon schema inputs and metadata.
12. **Every harness golden that digests the catalog or dumps a bank**,
    re-recorded on both OSes:
    - the CLI harness rows whose goldens carry a bank dump or an export
      (`rust/cli_harness/goldens/<row>.{linux,windows}.json`: transfer,
      invite and maintainer at least), from the candidate:
      `python rust/cli_harness --row <row> --record-candidate --candidate
      <shim>`, then `--write` (`rust/cli_harness/README.md`, "Goldens after
      the freeze");
    - the daemon `run.py` golden rows and the W2-E scenarios, from the
      candidate: `python rust/daemon/harness/run.py golden
      --record-candidate` and `w2e.py golden --record-candidate`, then
      `--write`;
    - dream cursors and the graph store use their `golden --record-candidate` commands, followed by `--write` after review. If a Rust-only change affects a principals golden and its recorder still records the frozen Python, stop and ask the maintainer before changing that golden.

## D. Before merge

13. Confirm the Rust workflow's schema and Parity checks are green at the integration batch head before merge.
14. A schema/DDL/migration change requires a local full suite (CLAUDE.md,
    "Running tests").
