### Fixed (2026-11-04 — Keep clean stress evidence after a setup-only retry)
- Stress reports accept one replacement per iteration when the jobs API proves its earlier attempt failed before the selection step ran. They retain every attempt and cause, reject test and later failures, and require the same number of actual clean selections.
