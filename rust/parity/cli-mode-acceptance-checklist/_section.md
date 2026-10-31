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
Audit and lease have explicit-DSN native subsets using the Rust PostgreSQL client; recovery remains deferred, as do the unsupported audit and lease shapes.

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

The Native column, after Status, says what the Rust binary (`rust/shim`) does with the mode on
master: `native` (every shape the row claims), `partly` (the ported sub-rows the
Evidence cell names run natively, other shapes defer by name before any effect)
or `defers` (every invocation prints the deferral and exits 1: the mode is in
`DEFERRED_MODES` in `shim/src/cli.rs` and has no native leaf). A mode's Status stays `deferred` until every sub-row closes; the
sub-rows' own status is in Parity rows. Audited at `75a8015e` (2026-11-01): 9
modes native, 15 partly and 5 defer. The count is of 29 logical modes, with
`conductor-watch` and its deprecated `delegate-watch` spelling sharing the
existing `delegate-watch` row; both defer and have no native leaf or scoped
sub-row yet. Check `shim/src/main.rs` as well as `shim/src/cli.rs`: the former
dispatches the native lease, board-audit, maintainer and sent-serve paths
before the latter's deferral list.

| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
