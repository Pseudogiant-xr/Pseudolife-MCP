# Rust port state

The Python oracle is pinned to `136a34ae95e981a691fcc31ba9fb4f35d83d4249`
(0.15.0, schema 52). This program is in phase 0; no production Rust surface
has been accepted and no phase is complete.

| Phase / item | Status | PR | Evidence |
|---|---|---|---|
| 0: Behaviour inventory and oracle selection | Drafted; review pending | — | `PARITY.md` |
| 0: Porting rulebook | Drafted with trial corrections; PR review pending | — | `PORTING.md` |
| 0: Differential harness and negative control | Corrected full-daemon replay and compiled negative CLI control recaptured; fresh review pending | — | `evals/rust_port/`; `evals/results/rust-port-phase0-selfcheck-r4/`; `evals/results/rust-port-phase0-full-bank-r5/` |
| 0: Python baseline and hosted CI measurements | Recaptured with repaired instrument; source hashes verified; fresh review pending | — | `evals/rust_baseline/README.md`; `evals/results/rust-rewrite-baseline-*-r5.json` |
| 0: Disposable three-unit trial and two adversarial reviews | Completed; lead reproduced both reviewers' counterexamples | — | Corrections recorded in `PORTING.md`; no trial code ships |
| 0: Independent PR review and publication | Initial findings repaired; replacement captures complete; fresh review pending | — | Readiness identity, process ownership, runtime provenance and instrument routing fixes pass the combined validation |
| 1: Stdio shim | Protocol proxy in progress | — | No production Rust parity accepted |
| 2: Client CLI leaves | Bounded prompt-hook unit in progress | — | No production Rust parity accepted |
| 3: Daemon read path | Deferred | — | Phase 0 artifacts must first be committed |
| 4: Daemon writes and background duties | Deferred | — | Read path and contract prerequisites |
| 5: Cutover and retirement | Deferred | — | Maintainer owns merges and deployment |

## Decisions

- 2026-10-03: The maintainer chose an external pytest plugin for implementation
  selection. Existing tests, including `tests/conftest.py`, remain immutable.
- Baseline host names use anonymous labels with hardware and software details;
  measured artifacts must not contain local account names or network addresses.
- The phase 0 evidence describes the pinned oracle, not subsequent upstream
  changes. The integration base advanced to `d3980687` (schema 53) after capture;
  accepting a different production schema target requires a maintainer decision.

## Verified gaps

- The configuration guide documents that the default Qwen model has no supplied
  ONNX artifact and falls back to torch. The phase 3 same-graph prerequisite and
  floating-point tolerance remain unverified; no bit-identical claim is accepted.
- Tests importing Python internals cannot demonstrate Rust wire parity merely by
  changing a file-level selection. The inventory must identify those boundaries.
- The isolated shim smoke with installed MCP SDK 2.1.1 negotiated `2025-11-25`
  after an initialize request for `2026-07-28`. This probes legacy negotiation;
  the modern `server/discover` path requires separate phase 1 wire evidence.

## Resume

Obtain fresh independent review of the repaired instrument and replacement captures,
then run the committed-head WSL suite and open the phase 0 pull request. Keep the recorded schema 52 oracle distinct from the schema 53 integration
base; do not reinterpret historical measurements as a new-source baseline.
