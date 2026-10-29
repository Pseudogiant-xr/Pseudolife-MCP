### Hook producer reduction preparation

The briefing/prompt-hook candidate remains deferred pending actual runtime
checks on the reduced source. `hook-strict-json-refusal`, `hook-typed-markdown`,
`hook-ascii-numeric`, `hook-first-line-cursor`, `hook-bounded-json-nesting` and
`hook-native-output-failure`
are deliberate substitutions described in PORTING.md, not byte-parity claims.

Numeric source evidence: `briefing_cli.py::run_briefing` declares argparse
`type=int`; `_fetch_markdown` URL-encodes the signed values unchanged.
`web/routes.py::_i` converts with `int`, and `service.py::session_briefing`
uses comparisons/slices without an upper bound or clamp. ASCII caps therefore
retain arbitrary magnitude and negative values; the 4300-digit interpreter
threshold and PYTHONINTMAXSTRDIGITS are removed. No installer-only bound or
max(0, ...) normalization is introduced.

The 18 existing subprocess hook admissions and original tests remain unchanged.
The port-owned COLUMNS and closed-output tests use the named candidate rules;
additive malformed-input, typed-markdown, beyond-u64, first-line cursor and
closed-output cursor controls are prepared. Historical Python diagnostics are
preserved by `cli_briefing_hook.expected`; `native_expected` is separate.
The help asset has a pinned Python drift guard at COLUMNS=80. The Windows
prepublication gate passed the debug build, format check, default/no-default
all-targets Clippy, 12 CLI unit tests and six hook integration tests. The first
disposable native prompt cell fetched a fixture note and advanced its cursor
before the remaining native checks ran. The first-line cursor unit controls
accepted `100.0\n\xff` as `100.0` and refused malformed first lines.

The executed `hook-bounded-json-nesting` unit control observed 127 nested
arrays accepted and 128 refused; a root object plus 126 nested arrays was
accepted and plus 127 refused. This is serde_json's default parser budget,
not a new application limit. The controls also preserved signed ASCII caps
of arbitrary magnitude, beyond-u64 ignored metadata and native output-failure
cursor state. These targeted Windows checks establish no corpus, timing,
full-suite, hosted or both-platform acceptance; the candidate remains deferred.

Master-forward integration of `59a8624e` retains these hook production modules
and original admissions unchanged, while adding the accepted lease source,
shared credential checks and dependency features. The synchronous entrypoint
starts a separate current-thread runtime only for the lease leaf; hook/version
dispatch remains before the proxy runtime. The combined source has 118 passing
offline checks, with its initial diagnostic mismatch retained separately.
Combined source `273e6533` passed Windows formatting, all-target check and
Clippy under default/no-default features, 19 CLI unit tests and 55 targeted
hook, dispatch, version and lease contracts. The initial contract failure from
an omitted existing oracle metadata setting is retained alongside the passing
rerun; source, tests and oracle were unchanged. Historical `64f5a6a2` execution
retains its original attribution. Fresh independent review, hosted checks,
the committed-head remote Python suite and remaining both-platform
proof are still required.
