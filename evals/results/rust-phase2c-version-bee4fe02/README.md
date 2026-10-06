# Frozen-head version capture at bee4fe02

These are actual Windows and Linux invocations at `bee4fe0269fe31069f24cf8ddcf122a908e0ead8`, tree `56df42f78cd6fd170c3dbdb6c55194cbe71ab63b`. Both use genuine Python 3.11 at oracle `3c01bb31abd60178e15dea99adda369b4bbf92fc`, package 0.17.0. Version remains deferred pending current hosted Rust/Parity and independent review; this packet makes no ported claim.

The retained release images were built at `74b5998c`: Windows SHA256 `808c5437cb01ba036e063533c33ee161c38580f4d83088923bf6caab6c16f4c4` (13,268,480 bytes), Linux `8a4c295f34f3121934605a25f6a86167e5bedea1b030f7d2800259a822c47908` (16,826,112 bytes). All 187 listed canonical implementation/instrument/test/workflow blobs match the executed bee4 source. The restored Windows target has different bytes and inherits no assertion or timing. Source equality permits retained-image reuse; the actual invocations, not equality alone, bind this capture to bee4.

Both OSes pass the default-installed end-to-end smoke, 26 exact CLI cases, 104 rejected exit/stdout/stderr/state controls, native invalid argv and all eight original CLI nodes once/non-skipped per arm. Windows invalid UTF-16 and Linux invalid POSIX bytes retain their platform-specific controls. Exact stdout, stderr, exit and installed environment/state checks pass. Windows lossless image references are SHA-checked; raw failures and full private streams remain retained.

The unchanged measurement helpers use three repeats of ten alternating samples per arm in each layout. Values below are milliseconds. Floors are the larger observed repeat floor across both arms; they are descriptive, not confidence intervals.

| OS/layout | Rust median / p95 | Python median / p95 | Rust minus Python median / p95 | Larger median / p95 floor |
|---|---:|---:|---:|---:|
| Windows bare | 20.556 / 22.483 | 151.031 / 174.903 | -130.475 / -152.420 | 5.141 / 3.802 |
| Windows installed | 286.480 / 320.302 | 176.449 / 197.194 | 110.030 / 123.108 | 11.695 / 52.388 |
| Linux bare | 2.581 / 2.854 | 126.947 / 131.129 | -124.365 / -128.276 | 1.784 / 1.886 |
| Linux installed | 3.110 / 4.016 | 599.200 / 623.718 | -596.090 / -619.702 | 7.633 / 2.569 |

The Windows installed 110.030 ms median regression exceeds its 11.695 ms floor, and its 123.108 ms p95 regression exceeds the 52.388 ms floor. Historical 278/156 ms and 74b fresh-copy help/version diagnostics remain intact; no new causal diagnosis or dismissal as noise is made. The line ratio remains 176/125 = 1.408, with the historical denominator and exclusions defined in the manifest. Timings include owned process setup, output collection and clean exit with warm filesystem caches; prep and validation are untimed. Linux's prepared oracle contains separately bound eval overlays while pinned production/source imports remain genuine; this is recorded as baseline_source_dirty, not a clean whole-directory claim.

The historical native Linux stdio attempt at `74b5998c` executed all ten nodes once/non-skipped in both arms, with five passes/five failures per arm and shared HTTP 500 responses. Only an unchanged Python idle-gap diagnostic established missing offline model files; the other four shared failures were not independently diagnosed. Tool refusal failed in both arms, while unknown-parameter refusal passed in both. No local stdio rerun or model download was added; current hosted Parity must resolve final acceptance.

`manifest.json` binds projection hashes and exact source blobs. Its separate inventory annotation records the exact unchanged `rust/phase1-test-buckets.json` blob, which is also included in the broader Linux capture binding. Every route, source identity, acceptance value, reason string and test assertion remains unchanged. The inventory phrase “not executed” describes only its historical prepared state; later 74b execution and current hosted acceptance are distinguished here and in the parity/state documents. The result/status-only carrier is bound externally after commit without relabelling the executed bee4 head, modifying capture helpers or transferring receipts to different image bytes.

Merged Phase 1 #560 at `465d75d9` has its own validated `a3e95642`, 14 passing checks and two-machine eight-route full-suite bindings; it does not verify this version head's 18 admissions. [Evidence-only PR #609](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/609) preserves the inherited ONNX instrument/receipt and complete later read-path map. Retained copies remain explicitly dispositioned here; original #589/#606 remain open until the surviving version/fixture publication is verified. Ranking remains withdrawn, no numerical tolerance is adopted, and strict manifest NaN/Infinity, lone-surrogate and beyond-u64 inputs remain deferred.
