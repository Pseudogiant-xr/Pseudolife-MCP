## CLI mode acceptance checklist

Source: `pseudolife_memory/cli.py` at `f709abb54f7912ae9cd767998d0926ca33df4bcd`.
The default is `shim`; help aliases are `-h` and `--help`, and version also accepts
`--version`. Dispatch reads the first argument; help/shim/channel ignore trailing
arguments. Every mode preserves exit status, both output streams and argument
validation. Subcommands remain part of each mode's contract.

The requested 19 Phase 2 modes are ten Tier A leaves, six Tier B modes and three
Tier C modes. Help/version are two carried Phase 1 gaps; channel is the shared
Phase 1/2 remainder. The original 26 plus upstream maintainer/test-login made 28
at the `f709abb5` pin; `delegate-watch` (#755, 2026-10-15) makes 29 on master
(`cli.py` at `98c5d6bf`). Tier labels record the requested scope, not a claim of HTTP-only
dependencies: recovery, audit and lease operator actions reach PostgreSQL directly.
Audit and lease have explicit-DSN paths using the Rust PostgreSQL client.
Native coordination recovery has its own row. #1699 completed the native
audit answers, including refusals for unsupported input.

The first slice implements help and unknown dispatch and adds a separate 15-case
CLI corpus, strict byte self-replay/candidate judge, environment selector and help
cold-start-to-exit measurement instrument. Current receipts from committed
`779c588c` on Windows and native Linux pass 15 cases, 45 controls and five
Python/five Rust original nodes each; both help 3x10 measurement pairs and
descriptive repeat floors are linked in PORT-STATE.md. Scope is UTF-8
stdout/stderr and valid Unicode scalar argv, preserving Windows CRLF and Linux
LF. Locale/default and other output encodings and non-UTF-8/surrogate argv remain
deferred. Help and scoped CLI-DISPATCH acceptance are ported: both-OS local
receipts, help pairs and all four Rust/Parity jobs in workflow 37245992895 pass.
CI receipts bind actual merge checkout `b5f485c9` to the same tree as PR head
`779c588c`; historical `6e936887` records remain unchanged. Standard file locks
replace `fs2`; source validation passed 277 Linux and 296 Windows Rust tests
without skips. Current documentation-head review/checks remain separate.
All nine reported PR checks passed at `779c588c`. The five existing
`cli-main-process` nodes remain the only routed CLI nodes. Other entries below
name concrete oracle nodes or internal test-function pools awaiting a process
adapter; a function name without parameter suffixes is not a routed node claim.

The Native column describes the inputs covered by each row. `native` or
`yes` marks native answers for that scope. `partly` marks a native subset.
The Status column records acceptance of the sub-rows in the parity register.
The November 1 inventory at `75a8015e` predates the sweeps.
At master `690bc4402` (18 November 2026), `DEFERRED_MODES` in
`shim/src/cli.rs` lists `tunnel` and `move`. The command table in
[`CUTOVER.md`](../../CUTOVER.md#what-is-left) describes when those modes still
reach the generic deferral. `conductor-watch` and its deprecated
`delegate-watch` spelling share one row. #1723 completed native answers for
both spellings. `shim/src/main.rs` also dispatches native coordination
recovery, lease, board-audit, maintainer and serve before the dispatcher in
`shim/src/cli.rs`.

| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
