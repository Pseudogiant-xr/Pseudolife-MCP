## Historical phase 1 paired measurement evidence

Paired measurements at `2e628b27` completed three repeats of ten samples per
arm on each platform. All 120 timing/RSS launches per platform exited cleanly.
The real Python SDK preflight remains inside Rust startup; its descendants
contribute to sampled process-tree RSS. Receipts are
`evals/results/rust-phase1-measurement-windows-2e628b27.json` and
`evals/results/rust-phase1-measurement-linux-2e628b27.json`.

| Platform | Metric | Python p50 / p95 | Rust p50 / p95 | Python floor p50 / p95 | Rust floor p50 / p95 |
| --- | --- | --- | --- | --- | --- |

Floors are observed ranges of three identical-input repeat-block quantiles,
not confidence intervals. Each arm retains its own source, runtime and
descriptive floors. RSS is a sampled lower
bound and can miss peaks between 5 ms polls. Size covers only the selected
executable, excluding Python runtime/dependencies; it is not deployment footprint.
The pair uses warm filesystem caches and an empty loopback fixture, with no
PostgreSQL, models, daemon spawn or coordination. Desktop activity is uncontrolled.
Historical r5 uses a different timing/RSS boundary and cannot substitute for
this pair. These source-bound measurements establish no general speed claim
or Phase 1 acceptance.
