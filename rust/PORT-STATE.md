# Rust port state

The Python oracle is pinned to `136a34ae95e981a691fcc31ba9fb4f35d83d4249`
(0.15.0, schema 52). Phase 0 is complete with reviewed PR #540 open;
GitHub CI is pending. No production Rust surface has been accepted.

| Phase / item | Status | PR | Evidence |
|---|---|---|---|
| 0: Behaviour inventory and oracle selection | Complete; BASE and RULES accepted, production rows deferred | [#540](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/540) | `PARITY.md` |
| 0: Porting rulebook | Complete with trial corrections | #540 | `PORTING.md` |
| 0: Differential harness and negative control | Reviewed full-daemon replay and compiled negative CLI control | #540 | `evals/rust_port/`; `evals/results/rust-port-phase0-selfcheck-r6/`; `evals/results/rust-port-phase0-full-bank-r5/` |
| 0: Python baseline and hosted CI measurements | Reviewed captures; historical source bytes reconstructable | #540 | `evals/rust_baseline/README.md`; `evals/results/rust-rewrite-baseline-*-r5.json`; `evals/results/rust-rewrite-source-reconstruction.json` |
| 0: Disposable three-unit trial and two adversarial reviews | Complete; both reviewers' counterexamples reproduced | #540 | Corrections recorded in `PORTING.md`; no trial code ships |
| 0: Independent PR review and publication | Review approved; committed-head WSL suite passed; PR open | #540 | Tested source commit `5f4152bbee7424bbbcfee63f1283a39810e34afd`; validation in PR body |
| 1: Stdio shim | Blocked on MCP-WIRE and SHIM-LIFECYCLE parity; bounded prototype under repair | — | No production Rust parity accepted; full phase oracle CI and performance evidence remain absent |
| 2: Client CLI leaves | Blocked on CLI-HOOK and remaining CLI rows; bounded prompt-hook prototype under repair | — | No production Rust parity accepted; full phase oracle CI and performance evidence remain absent |
| 3: Daemon read path | Prepared only; implementation not started | — | Phase 0 artifact-commit prerequisite satisfied; HTTP-SECURITY is the first bounded unit |
| 4: Daemon writes and background duties | Deferred | — | Read path and contract prerequisites |
| 5: Cutover and retirement | Deferred | — | Maintainer owns merges and deployment |

## Decisions

- 2026-10-03: The maintainer chose an external pytest plugin for implementation
  selection. Existing tests, including `tests/conftest.py`, remain immutable.
- Baseline host names use anonymous labels with hardware and software details;
  measured artifacts must not contain local account names or network addresses.
- The phase 0 evidence describes the pinned oracle, not subsequent upstream
  changes. The integration base is `d3980687` (schema 53);
  accepting a different production schema target requires a maintainer decision.

## Verified gaps

- The configuration guide documents that the default Qwen model has no supplied
  ONNX artifact and falls back to torch. The phase 3 same-graph prerequisite and
  floating-point tolerance remain unverified; no bit-identical claim is accepted.
- Tests importing Python internals cannot demonstrate Rust wire parity merely by
  changing a file-level selection. The inventory must identify those boundaries.
- The isolated shim smoke with installed MCP SDK 2.1.1 negotiated `2025-11-25`
  after an initialize request for `2026-07-28`. This probes legacy negotiation;
  the modern `server/discover` path has only bounded experimental evidence.

## Resume

Check PR #540 CI before maintainer merge. Complete the deferred phase 1 and 2
parity rows, including their oracle CI and measurement artifacts; the experimental
units do not establish phase completion. Keep the recorded schema 52 oracle
separate from the schema 53 integration base, and preserve historical measurements.
