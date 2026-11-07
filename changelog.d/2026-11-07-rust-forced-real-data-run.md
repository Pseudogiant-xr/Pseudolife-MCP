### Added (2026-11-07 — the Python-vs-Rust differentials can run on a copy of the real bank)
- `rust/daemon/harness/mirror/forced.py` runs every live w2e, w2d_search, board and route scenario, Python against Rust, on the mirror's seeded copy of a nightly backup, one harness process per scenario. A scenario that differs runs again on the real bank and then on its synthetic bank, which classes it `transient`, `preexisting` or `new` (real data only). The report holds counts and names; the differences stay on the host.
- `ops/Dockerfile.mirror-runner` pairs one release's Python image and Rust binary on the mirror's internal network.
- Harness support, each inert unless its variable is set:
  - `PL_HARNESS_BASE_DATABASE`: new disposable databases copy a `pl_cf_mir_*` bank, and `dbstate` dumps hold only the rows that differ from it. A whole 284 MB bank per step exceeded 9.6 GiB.
  - `PL_HARNESS_STUB_DEFAULT_CONTENT`: the stub extractor answers unmatched prompts identically for both daemons, recorded as `defaulted`.
