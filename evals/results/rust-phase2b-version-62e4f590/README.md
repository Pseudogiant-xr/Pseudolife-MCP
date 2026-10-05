# Native version evidence at `62e4f590`

The Windows installed layout regressed: Rust's median was 278.43 ms versus
Python's 156.21 ms, a 122.22 ms slowdown beyond the 5.47 ms observed median
floor. Windows bare and both Linux layouts improved beyond their respective
floors. These observations do not identify a cause or establish an aggregate
speedup across platforms.

These local release receipts retain measured commit
`62e4f590c73a43c8bde0ae9aba55710208cccee3`, tree
`ab2d84ec76db706b8dbc4e6252d822f4ada05600`. The Python oracle is
`eb0c13e9c5036aa2b95e7fccb77f41ca1c095493`, actual package/distribution
0.17.0 and schema 55. Capture runtimes differ: Windows CPython 3.11.9 and
Linux CPython 3.11.15. Each receipt binds the capture producer's runtime executable,
source, candidate binary and executed helper hashes. Windows release SHA256
is `64193340ed20df5dc0183e761043e57616a848ee4184d352865d23680d96874b`;
Linux release SHA256 is
`8222c4a1105b636afd5415324ba0d119cebb886e92bef423c0e0d95ca6ec1f2c`.

[Windows corpus](cli-corpus-windows.json) and [Linux corpus](cli-corpus-linux.json)
are byte-identical copies of the reviewed allowlisted projections. Each records
26 passing cases (five help, ten unknown dispatch, eleven version) and 104
rejected candidate recorded-output mutations, including file-state controls.
The original raw comparisons remain private; [manifest.json](manifest.json)
binds their hashes and byte counts. Projections preserve the complete case
inventory, provenance, controls and owned daemon/database cleanup.
There are no comparison normalizations.

The Linux corpus needs a runtime qualification: its fifteen help/unknown-dispatch
oracle children invoked the base interpreter after the fixture resolved the
virtualenv executable's symlink. The recorded 26 byte matches and 104 rejected
controls remain factual for those executions, but the fifteen rows do not prove
parity against the admitted virtualenv. `capture_runtime` identifies the capture
producer, not each child. The eleven version rows, Windows corpus, all four
measurement cells and both current startup captures are unaffected by this
defect. Original receipts and counts are retained; recapture of the fifteen Linux
rows after the fixture repair remains pending.

Version cases cover bare `version`/`--version`; installed default commit,
flag with ignored tail, requirement and checkout without commit; paired root/
launcher overrides; each half override; and the wrong platform launcher suffix.
Installed preparation uses genuine minimal virtualenv relocation, preserving
real `sys.prefix`, distribution identity and executable bytes, with identical
Python/Rust files seeded in each reset home. The running executable resides in
its own runtime child and reads that runtime's complete installer-format
manifest. A newer complete sibling tests that the running runtime remains the
identity. No interpreter or package identity is substituted.

Installed manifests are restricted to the approved installer-written schema.
Captured NaN/Infinity and lone-surrogate differences remain explicitly deferred;
these receipts do not claim arbitrary JSON-manifest equivalence. Streams are
UTF-8 with valid Unicode scalar arguments; Windows CRLF and Linux LF remain
distinct. Other encodings, non-scalar arguments and other CLI modes remain
outside this evidence.

The four measurement receipts contain all 30 timing samples per arm, original
metrics, repeat floors, two untimed control validations and cleanup counts:
[Windows bare](measurement-windows-bare.json),
[Windows installed](measurement-windows-installed.json),
[Linux bare](measurement-linux-bare.json), and
[Linux installed](measurement-linux-installed.json).
Installed timing uses the default commit case in a stable shared home, reset
outside the timer. Bare timing prints only the package identity line. Both
layouts use the unchanged committed measurement instrument, with three repeats
of ten alternating paired process starts per arm: 240 timed samples and eight
untimed controls in total.

| OS | Layout | Python median ms | Rust median ms | Python minus Rust ms | Larger median floor ms | Python p95 ms | Rust p95 ms | Larger p95 floor ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Windows | bare | 132.79 | 19.85 | 112.94 | 6.78 | 157.09 | 36.14 | 14.71 |
| Windows | installed | 156.21 | 278.43 | -122.22 | 5.47 | 175.73 | 309.36 | 21.29 |
| Linux | bare | 198.49 | 1.34 | 197.15 | 5.65 | 212.19 | 2.13 | 17.84 |
| Linux | installed | 588.19 | 1.66 | 586.53 | 5.14 | 604.98 | 2.09 | 6.27 |

Pooled distribution medians use the ordinary median; p50/p95 use nearest-rank
quantiles over 30 samples. Each floor is max minus min of the three repeat-block
medians or corresponding quantiles. The comparison uses the larger arm floor;
these observed ranges are descriptive, not confidence intervals. Windows
installed p95 also regressed by 133.63 ms, beyond the 21.29 ms floor.

Timing includes owned-process setup, output collection and clean exit with warm
filesystem caches; preparation/reset, snapshots and file checks are untimed.
Untimed controls require exact stdout/stderr/exit and installed file/environment
equality. Every timed result must match its control. All measurement-owned homes
and processes were removed. Repeat admission recorded free local locks and fresh
independent resource clearance while the CLI board adapter was unavailable.
No daemon, database or models were used for timing; GPU execution was disabled.

The selected oracle's aggregate `provenance.source_dirty` is retained as true:
its evaluation helper bridge differs from the oracle commit. Production/tests/
pyproject match the selected pin, the bound production ownership is clean, and
the actual loaded instrument is clean at `62e4f590`. These distinct checks must
not be interpreted as an entirely clean oracle checkout.

Native version source adds 172 physical Rust lines excluding tests/assets:
157 in `rust/shim/src/cli/version.rs` before `#[cfg(test)]`, plus 15 added
shared dispatcher lines in `rust/shim/src/cli.rs` versus `c9705e88`.
The pinned Python mode module is `pseudolife_memory/cli.py`: 223 physical lines
minus its 98-line AST `_USAGE` asset gives a 125-line denominator and ratio
**1.376**. Counts include comments/blanks. Python has no separate version module;
its `_print_version` function alone spans 14 lines and is not the module
denominator. Shared runtime support, tests and external assets are excluded.

Fresh independent code review approved `62e4f590`. CLI-VERSION remains deferred
pending required hosted CI and independent review/checks of the evidence/doc
successor. [Rust run 37310594788](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37310594788)
passed both Rust jobs but failed both Parity jobs in the synthetic PYTHONPATH
negative fixture (150 tests passed per OS). The real venv guard ran before the
fixture's intended rejection; differential judges were skipped. A test-only
seed collaborator repair passes four targeted checks in both ordinary and
genuine-venv interpreters. Corrected hosted acceptance remains pending.
Historical refusals and earlier captures retain their original identities.

[Rust run 37315503446](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37315503446)
at `ac0c64a1` passed both Rust jobs and 151 CLI fixture checks per platform.
Both Parity jobs then failed the Phase 1 judge with six differences each:
two stale 0.16.1 startup stderr expectations against both 0.17.0 arms, and
four generated registration-name differences between Python replay arms.
All ten selected public stdio nodes passed per platform; the later version
judge was skipped. The new [startup proof](../rust-phase2b-startup-eb0c13e9/windows.json)
and [Linux startup proof](../rust-phase2b-startup-eb0c13e9/linux.json) bind a
literal 0.17.0 contract to complete storage-free captures, with the frozen
f709/0.16.1 contract retained separately. Derived-name identity substitution
remains unapproved and its comparison policy is unchanged. Hosted acceptance
and fresh independent review of the repair remain pending; CLI-VERSION stays
deferred. The earlier run and local measurement records above remain historical.

## Bounded Linux follow-up at `804e829e`

The [fifteen-row recapture](../rust-phase2b-version-804e829e/README.md) now passes
all five help and ten unknown-dispatch pairs against the admitted virtualenv,
with all sixty candidate output/file-state controls rejected. It uses the
reviewed lexical-invocation repair at clean `804e829e` and the unchanged original
`62e4f590` release ELF. The pending recapture statements above describe the
historical packet before this follow-up. Original receipts, the erratum,
eleven version rows, Windows corpus, timings, startup captures and hosted
failures remain intact. This bounded receipt is not a new complete 26-row gate;
CLI-VERSION, hosted acceptance and derived-name policy gates remain unchanged.
