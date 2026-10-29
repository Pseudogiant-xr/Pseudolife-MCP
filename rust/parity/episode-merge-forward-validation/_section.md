## Episode merge-forward validation boundary

Accepted lease master `59a8624e` is a parent of episode source merge
`77fec51ee7077c12c60f419ea0c478e8b8e84c9a`. Its Windows default/no-default
fmt/check/all-target Clippy and affected CLI library/integration tests passed;
67 Rust tests ran per configuration. The shared process/measurement selection
passed 256 tests with 12 skips. The initial `e170d674` instrument failure and the
one-line optional-file repair remain separately attributed in PORT-STATE.md.
Episode leaf source remains byte-identical to `d2e86010`, and accepted master
lease/version production inputs are retained. CLI modules are registered together
at `shim/src/cli.rs:6–7`; `main.rs:7–13` retains episode routing before opaque-tail
conversion, `:22–33` retains lease routing, and `:34–38` preserves ordinary
help/version dispatch. Measurement callbacks/readback remain episode-only;
lease check/list retain their own exact TOKEN_FILE fixture and post-state checks.

The d2e box full suite and historical native174/capture639 measurements retain
their original source/image identities. This merge has no new timing claim or
Linux/full-suite result. The `d2e86010` full suite (18,203 passed, 76 skipped,
exit 0) is accepted for the unchanged episode leaf. Fresh independent review
and hosted merge-ref CI remain pending. No parity row is
promoted; both episode modes remain deferred. This documentation successor changes
no runtime/instrument input and does not relabel execution as a successor run.
