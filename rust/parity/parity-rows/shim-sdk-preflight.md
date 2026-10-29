| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| SHIM-SDK-PREFLIGHT | 1 | Python-only MCP SDK import preflight is retired in the Rust candidate by the maintainer's 2026-10-05 phase 2 decision, section 1; no Rust SDK diagnostic counterpart. `PORTING.md` no-python-before-first-frame | `test_shim.py` SDK guard nodes remain Python oracle tests; the three manifest rows explicitly record the decision | retired-by-decision |
