| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| BASE | 0 | Commit/host/bank/run-count RSS idle and workload, shim cold start, search/store/fact-set and send/receive p50/p95; CI lane timing. `evals/`, `.github/workflows/ci.yml` | `tests/test_eval_evidence.py`, `test_report_redaction.py` A; current-pin Linux scaling in `evals/results/rust-phase0b-daemon-scaling-linux.json`, fixed-reference CI noise in `evals/results/rust-phase0b-ci-same-head.json`; historical #540 shim/write evidence retains its original pin | ported |
