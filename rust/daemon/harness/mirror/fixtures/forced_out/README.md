# Recorded harness runs for `forced.py`'s rebuild

`test_forced.py::test_a_recorded_harness_run_rebuilds_to_the_lines_it_printed`
reads these. Each pair is one real harness run on the harness's own
synthetic bank: `<suite>--<scenario>.json` is the `--out` JSON the harness
wrote, and `.log` is its merged output as `Runner.invoke` keeps it. They hold
the harnesses' seed data only, never a real bank's text.

| Files | Source | Printout |
|---|---|---|
| `w2e--console-graph-malformed` | the forced run's synthetic arm at `8eb08c214` | nothing cut |
| `w2d_search--mcp-search-explain` | the same | a 12-item case line cut to 5 |
| `w2d_search--rrf-pool` | the same | case lines cut to 5 items, 342 DB lines to 20 |
| `routes--seeded` | the same | nothing cut |
| `routes--trust-bind` | the same | nothing cut (a scenario error) |
| `board--identity` | `board/run.py live --only identity` at `f52ee4130`, its Rust daemon built with `--features mutants` and run with `PSEUDOLIFE_DAEMON_MUTANT=board-canonical-unsorted` (the forced run's synthetic board arm never differed) | 24 CHAIN lines cut to 12 |

None of the four harnesses changed its DIFF print or its final `--out` write
between `8eb08c214` and `f52ee4130`;
`test_the_rebuild_caps_are_the_harnesses_own_print_statements` pins both.
When either changes, record a new run of that harness here, copying
`<work>/<suite>/<label>/<scenario>.json` and `.log` as the runner left them.
