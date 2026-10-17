### Fixed (2026-10-17 — mutant controls no longer fail on a reused target dir)

- The CLI harness's mutant control (`python rust/cli_harness --mutants`) now keeps its copy of `rust/` under `$CARGO_TARGET_DIR/mutant-src` exact: files and directories the checkout no longer has are removed before each sync. A target dir reused across master moves had kept a stale `shim/src/cli/doctor.rs` beside the new `doctor/mod.rs`, so every mutant build failed with rustc E0761 and was reported as "mutant build failed".
