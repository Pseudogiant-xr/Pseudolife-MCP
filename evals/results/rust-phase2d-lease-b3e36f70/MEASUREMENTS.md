# Producing the bounded lease measurements

This artifact contains every published timing sample, its repeat/sample/order, and statistics recomputed from those samples. It records the actual successor head/tree, immutable image hashes and producing debug build commands. Paths in build commands are portable variable aliases; the source archive method and canonical committed source hashes are retained. A rebuild at a different archive path may change debug-image bytes.

The two controller files and fresh-credential helper are exact bytes used for the four recorded final measurements. Use Python 3.11 with the genuine pseudolife-mcp 0.17 runtime and pinned production source. Supply an oracle checkout as `$ORACLE_ROOT`, with its corresponding installed runtime in its sibling `runtime` directory; production bytes must pass `require_phase1_source` for phase pin 3c01bb31/schema55. Use the committed candidate checkout as `$CANDIDATE_ROOT`. No new profile or layout is introduced.

Create an image packet with `candidate_head`, `candidate_tree` and `platforms.windows|linux`, each containing `image`, `candidate_sha256`, profile/build/source attestation. Values must describe the actual locally produced image; do not reuse published hashes for a different image. The recorded build recipe is `cargo +1.94.0 build --manifest-path $SOURCE_ARCHIVE/rust/Cargo.toml --locked --offline -j2 -p pseudolife-stdio --bin pseudolife-stdio`, with `nice -n10` on Linux. Source archive is the exact committed `rust` tree, preserving canonical lease asset bytes.

After checking the shared full-suite resource, use a fresh UTC clearance. From a quiet CPU window, execute each existing admitted controller separately:

```text
$PINNED_PYTHON -B phase2d-lease-final-check-measurement-driver-v2.py --host $HOST --candidate-root $CANDIDATE_ROOT --oracle-root $ORACLE_ROOT --expected-head $HEAD --expected-tree $TREE --candidate-image $IMAGE --image-packet $IMAGE_PACKET --credential-helper phase2d-lease-final-fresh-fixture-credential.py --offline-resource-checked-at $ACTUAL_UTC --repeats 3 --samples 10 --out $NEW_CHECK_RESULT
$PINNED_PYTHON -B phase2d-lease-final-list-measurement-driver-v2.py --host $HOST --candidate-root $CANDIDATE_ROOT --oracle-root $ORACLE_ROOT --expected-head $HEAD --expected-tree $TREE --candidate-image $IMAGE --image-packet $IMAGE_PACKET --credential-helper phase2d-lease-final-fresh-fixture-credential.py --offline-resource-checked-at $ACTUAL_UTC --repeats 3 --samples 10 --out $NEW_LIST_RESULT
```

On Linux set `LEASE_NATIVE_EVIDENCE` to an existing owned native evidence directory, put each result directly in it, and invoke with `nice -n10`; use the already installed Pg0 library path. Windows controllers choose BelowNormal priority. Existing retired-credential rejection was checked during the private runs using the optional `--retired-credential-log`; reconstructing the measurement does not require that historical log. No password or token is included in this artifact.

Both arms run an untimed exact control before measurement and one untimed warmup each before every repeat, then 30 alternating starts per arm. Fixture/reset/home/environment/state checks and independent empty-board probes are untimed. Each actual driver verifies exact streams/state and four unrelated-output mutations, stops owned daemon/children/PG, drops the disposable database and removes owned homes/data. Successful-run evidence is functional only.

The published `cold_start_to_exit_ms` key measures complete fresh process exit after image warmups. Recompute cells with the candidate's `evals.rust_baseline.shim_measurement.metric_cells(samples_for_one_arm, ('cold_start_to_exit_ms',))`. The p50/p95 floor is maximum minus minimum of the three repeat-block quantiles. These descriptive ranges are not confidence intervals or a release-performance claim.
