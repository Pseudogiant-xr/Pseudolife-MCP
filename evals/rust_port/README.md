# Rust port differential instrument

The instrument records whole CLI processes and HTTP/MCP responses and compares
an independently observed candidate. Phase 0b pins Python production source to
`3691f5cb75487d3fda54a6bde6fab35dcf32c681`, schema 53; evaluation code may
change independently and every receipt fingerprints it. Existing `tests/` and
production code remain unchanged.

## Current evidence and its limits

The combined acceptance receipt is `evals/results/rust-port-phase0b-acceptance.json`;
it binds zero-difference Python command/URL lanes, every named proxy control,
and compiled garbage Rust rejection to the hashed platform capture receipts.

Current Linux selfcheck, Linux full-bank and Windows CLI receipts are under
`evals/results/rust-port-phase0b-selfcheck-linux/`,
`evals/results/rust-port-phase0b-full-bank-linux/` and
`evals/results/rust-port-phase0b-cli-windows/`. Linux uses a native LF checkout
of the pinned oracle; Windows keeps its own CLI capture. Receipts name the OS,
architecture, source pin, schema, dependencies, instrument fingerprints and
resource admission. Replay refuses a missing or mismatching capture platform.

The selfcheck covers two CLI cases, four HTTP cases and five MCP cases above a
synthetic in-memory service seam. Its service does not exercise storage,
embedding, durable identity, background work or full daemon lifecycle. The
full-bank corpus covers 25 cases on fresh PostgreSQL banks with the real CPU
embedder: bearer rejection, MCP negotiation/list/call, storage, search, fact
supersession/history and addressed coordination mail. All banks are generated,
never copied. Python versus Python proves the instrument can replay this
corpus; it does not establish Rust implementation parity or complete coverage.
The deliberately broken Rust executable fails the CLI selfcheck. No production
surface becomes ported merely because a control passes.

Seven graded controls pass responses through a small owned loopback proxy in
front of the real Python daemon, changing one response per proxy: missing key
(`field_presence`), reordered ranking (`ranking_order`), milliseconds epoch
(`epoch_unit`), integer changed to float (`type`), error marked success
(`tool_error_envelope`), changed tool description (`value`), and duplicate JSON
member (`duplicate_json_key`). Each has a unit test and the full-bank receipt
records the observed differences, preserved status and proxy cleanup. These
controls make specific near misses load-bearing; they are not an exhaustive
wrong-port detector. Controls use search/list/error requests on the already
seeded oracle bank. Searches can update access counts and cortex state; receipts
retain those additional differences and require the intended rejection at its
named path.

Historical schema-52 selfcheck R6 and full-bank R5 remain unchanged. Selfcheck
R4/R5 were archived outside the working tree and remain recoverable from PR
#540's original Git history at `f2ee1524`. The source-reconstruction manifest retains the
recorded source digests and line-ending recipes for historical evidence; these
are not current-oracle results.

## Real candidate entry points

`full_bank run` supports an already running `--candidate-url`, an owned
`--candidate-command-json` argv, or `--python-candidate` to prove the owned
command path using Python. The oracle and candidate arms each receive a freshly
minted guarded PostgreSQL database and identical seed requests. No target DSN
argument is accepted. The runner exclusively creates and drops the banks, uses
private homes/configuration, hides CUDA, pins one-thread fp32 CPU embedding and
permits only an explicit read-only offline model cache. Live credentials and
installed settings never flow into a child. Runtime credentials stay private;
HTTP registration identities are normalized bijectively and credential echoes,
including in raw MCP bytes, refuse transcript publication.

An owned candidate receives `PSEUDOLIFE_MCP_DATABASE_URL`,
`PSEUDOLIFE_MCP_HOST`, `PSEUDOLIFE_MCP_PORT`, `PSEUDOLIFE_MCP_TOKEN`,
`PSEUDOLIFE_MCP_CONFIG` and `PSEUDOLIFE_BASELINE_NONCE` through the isolated
environment. Its evaluation readiness adapter must expose `/health` with
`status: ok` and `baseline_instance: {nonce, pid}` using that nonce and its
actual runtime PID. Ownership is established before process execution, and the
runner verifies PID membership in its Job/process group before accepting
readiness. All children are reclaimed on success, timeout and startup failure;
no candidate executable paths or argv secrets are published in receipts.

An external URL must be a loopback HTTP origin with an evaluation-only bank
adapter and its private `--candidate-nonce`. Before disclosing bank credentials,
the runner GETs `/_rust_port/ready` and requires exactly
`{nonce: PRIVATE_ADAPTER_NONCE, disposable: true}` from the separately launched
adapter. A nonce mismatch refuses binding before sending any DSN. The runner POSTs `/_rust_port/disposable-bank` with its minted
`database_url`, synthetic `token`, `nonce`, `bank_sha256` (SHA256 of the minted
bank name) and `disposable: true`. Before corpus requests the adapter must
respond exactly with `{nonce, bank_sha256, disposable: true}` after selecting
that empty bank. In cleanup it must acknowledge
`/_rust_port/disposable-bank/release` with `{nonce, released: true}`. The bank is
then dropped; the externally owned server is left to its caller. A bare server
URL without verified binding is refused. This is an explicit candidate adapter
contract, not a new production API.

```sh
python -m pytest evals/rust_port -q
python -m evals.rust_port.full_bank prepare --oracle-root /path/to/pinned-oracle --out-dir /tmp/prepared
python -m evals.rust_port.full_bank run --oracle-root /path/to/pinned-oracle --out-dir /tmp/full-bank --offline-resource-checked-at VERIFIED_FREE_UTC --python-candidate --validate-controls --validate-url-candidate
python -m evals.rust_port.full_bank run --oracle-root /path/to/pinned-oracle --out-dir /tmp/candidate --offline-resource-checked-at VERIFIED_FREE_UTC --candidate-command-json '["/path/to/candidate"]'
python -m evals.rust_port.full_bank run --oracle-root /path/to/pinned-oracle --out-dir /tmp/external --offline-resource-checked-at VERIFIED_FREE_UTC --candidate-url http://127.0.0.1:19002 --candidate-nonce PRIVATE_ADAPTER_NONCE
rustc evals/rust_port/broken_fixture.rs --edition 2021 -C codegen-units=1 -o /tmp/broken
python -m evals.rust_port.selfcheck --oracle-root /path/to/pinned-oracle --broken-binary /tmp/broken --out-dir /tmp/selfcheck --offline-resource-checked-at VERIFIED_FREE_UTC --validate-plugin
python -m evals.rust_port.selfcheck --oracle-root /path/to/pinned-oracle --broken-binary /tmp/broken --out-dir /tmp/cli --offline-resource-checked-at VERIFIED_FREE_UTC --cli-only
```

Resource clearance is checked with `pseudolife-mcp lease check full-suite`
before captures and every full-bank arm. Busy means refusal. When CLI board
access is unavailable, `--offline-resource-checked-at` records independently
verified local/WSL locks and peer clearance; it does not invent a board check or
bypass a held lease. Coordinate the CPU slot before model loading. One-thread
fixture compilation is bounded and on Windows runs below normal priority.
Every artifact uses exclusive create and cannot overwrite earlier evidence.

## Comparison policy

The recorded header allowlist is `content-type`, `content-length`,
`cache-control`, `location`, `www-authenticate`, `allow`, `retry-after`,
`mcp-protocol-version`, `mcp-session-id` and `x-pl-board`. Deliberately set
application/SDK headers are compared, including absent versus present. Session
IDs become per-session symbols after continuity checks. `content-length` is
retained for fixed framing and omitted from comparison only for an explicitly
observed fixed-versus-chunked pair. `content-length-authorized-wire-spans`
first validates the declared length against entity bytes, then adjusts only
the exact disjoint tokens changed by the declared identity, clock or source-text
line-ending normalization.
Nested MCP JSON adjustments retain the original outer escaping. The transcript
records raw length, adjustment and compared length; all bytes outside those
tokens still count, including whitespace and Unicode. Whole-body serialization
never determines the adjustment. This establishes normalized length parity,
with the raw lengths available for inspection. Date and Server are incidental server
metadata outside this contract. Redirects and environment proxies are refused.

CLI stdout/stderr are stored as base64 and compared byte-exact with the ordinary
integer exit code (0 through 255) on the captured platform. Signals and Windows
exception termination are boundary failures. `source-text-lf` normalizes CRLF
and lone CR to LF only at the named MCP tool-description paths. No broad
whitespace or string-case normalization is applied.

Clock normalization is declared per operation: epoch symbols preserve JSON
integer/float type, seconds versus milliseconds unit class and decimal order of
magnitude. Relative constraints and write/read identity continuity are checked
before normalization; a milliseconds epoch cannot pass as seconds. Booleans,
nonfinite values, missing declared fields and changed reference types fail.
Fact versions, message order, recipient sequences, cursor positions and ranking
IDs remain exact. Human age strings and compact dates remain exact, so crossing
a rendering boundary produces a difference.

JSON object key order is ignored; array order and multiplicity remain exact.
Score tolerance is opt-in at explicit JSON Pointer paths (default absolute
`1e-6`, relative zero), with one-segment wildcards. Tolerance never masks ranking
order. Embedded MCP JSON text is parsed only at declared paths and compared
alongside structured content. Duplicate members are rejected before top-level,
SSE-envelope or embedded-text parsing discards them. Raw MCP response bytes are
retained as `raw_mcp_body_b64` beside the parsed form and excluded by the named
`raw-mcp-retained-not-compared` rule; future stricter byte policies can use them
without recapture. SSE envelope arrival order remains exact; event framing
IDs/retry directives are not graded.

`oracle_tests.json` maps five existing CLI assertions to an external pytest
adapter. Candidate mode refuses unmapped selected nodes with usage error 4;
original assertions and SystemExit expectations are unchanged. Broader CLI,
shim and storage routing remain pending. Protocol negotiation records what the
installed MCP SDK actually supports; initializing a requested July-2026 protocol
that falls back to an older revision does not establish modern support.
# Phase 1 stdio judge

Run the bounded shim judge from a checkout installed with `[dev,lite]`:

```sh
python -m evals.rust_port.phase1 --candidate-json '["/absolute/path/pseudolife-stdio"]' --candidate-root /absolute/candidate-checkout --out /private/phase1.json --public-out evals/results/phase1.json --offline-resource-checked-at VERIFIED_UTC_TIME
```

Omit `--candidate-json` for Python self-replay. The command selects eight public
stdio test functions without changing their assertions, runs both protocol eras
on fresh guarded disposable PostgreSQL banks, and exercises EOF, refusal,
credential rotation, recovery and graded forwarding controls. PostgreSQL must
already be available through the existing `tests.pg_defaults` resolver or
`PSEUDOLIFE_BENCH_ADMIN_URL`. The clearance timestamp is the lead's independent
resource check; the runner also checks the local full-suite lease. No full test
suite runs.

Receipt schema 2 records each selected node's outcome from pytest's private
JUnit report. Exit zero alone is insufficient: skipped, errored, failed,
missing, duplicate or unexpected nodes cannot produce a passing receipt.
The public summary retains node identities/outcomes and the XML hash; raw test
output, error messages and XML stay beside the private receipt.

`--candidate-root` names the native Git checkout that holds the committed Rust
source, independently of `--oracle-root`. It must resolve on the OS running the
judge; a Windows checkout path with unavailable Git metadata is not a Linux
source identity. Receipts record its commit/tree, clean source hashes and binary
SHA-256, and bind the process tests and every candidate real-bank, EOF, fault,
startup and concurrent-call cell to that executable. The source/binary pairing
is not a build attestation: retain the build evidence separately. Python
self-replays bind the interpreter and
the checked production oracle source instead.

All judge sections must run for a complete passing receipt. The `--skip-*`
switches produce incomplete evidence. Process-test reuse refuses legacy
receipts, differing platform/oracle/instrument fingerprints, missing per-node
passes or a different executable. Final Python self-replays run fresh without
reuse or skip switches, once per platform after the instrument is frozen.

For a CI runner with `[lite]` installed and no prestarted PostgreSQL, use
`python -m evals.rust_port.phase1_ci` with the same arguments. It provisions a
fresh owned embedded instance through the existing provider, supplies disposable
test/admin URLs internally, and stops the listener before writing
`<private-receipt>.postgres.json`. It never prints database credentials.

`--port-stdio-json` and `PSEUDOLIFE_PORT_STDIO_JSON` select the candidate for the
external pytest adapter. Unmapped functions and private Python entrypoints are
rejected. Without these switches, tests retain their Python default. Only public
default, `shim` and `channel` launches can be replaced.
The stdio harness binds `PSEUDOLIFE_MCP_PYTHON` to its own `sys.executable`;
`env_extra` may override it, including with an empty value. The external stdio
adapter preserves a selector in the test's child environment, otherwise uses
the explicitly configured caller selector or its own interpreter. The CLI
adapter uses that same caller-or-interpreter choice. These bindings do not
change remote or no-spawn controls. The Parity workflow runs the instrument
with the prepared oracle interpreter and explicitly selects it for children.
For a full-suite gate, add `--port-full-suite` with `--port-stdio-json`:
all mapped stdio nodes use Rust, and every other collected node runs its original
Python assertions. This mode reports both counts and refuses a selection with
zero mapped stdio nodes. The default explicit selection still rejects unmapped
nodes; neither mode skips or deselects assertions.

The `stdio-raw-compared` policy retains and compares stdout bytes, including
member order, spacing, integer spelling and line framing. It changes only named
source newline spans at `/body/result/tools/*/description` and
`/body/result/instructions`. Stderr's allowlist is empty and exit codes are exact.
Only the real-bank corpus opts into `completed-readiness-wait-notice`: after
stdout matches under the existing comparison and both processes exit 0, stderr
may differ between empty bytes and the sole exact no-spawn, five-second wait
notice for `http://127.0.0.1:<port>` (1–65535). One terminating LF or CRLF is
required; extra lines, logs or altered text fail. Both arms must retain genuine
process provenance. Applied events include both raw stderr captures in
`normalizations_applied`, including in the public summary; missing evidence makes
the receipt incomplete. The capture streams remain unchanged. Startup, fault,
EOF and concurrent-call policies do not enable this rule.
`eof-observed-final-pair-orders` permits only the observed final two connection
errors to swap; their bytes, multiplicity and all preceding frames stay exact.
Each platform supplies its own byte oracle. Frozen Windows observations authorize
only the observed ID order sets on Linux, not stream newline normalization.

Generic HTTP controls use the exact per-case comparison Policy used for the
real candidate, including embedded JSON and ranking checks. Malformed responses
pass through the shared boundary observation wrapper and generic comparator.
The identity proxy seeds a fresh bank and must make zero mutations and produce
zero differences. HTTP raw bodies remain retained but ignored until Phase 3.
Deterministic test embeddings establish protocol and storage execution, not
model or retrieval parity. Historically, at `2e628b27`, all 125 scoped Phase 1
internal functions named Rust targets with Windows/Linux assertion evidence.
The current mapping contains 121 completed equivalents, exactly 3 SDK cases
retired-by-decision, and 1 pending postframe update-scheduling substitution.
The pending row now records targeted Windows/Linux evidence at frozen tree
`a4ff3236eb349aaed427d80129513fe22cf0183f`; Windows retains its transient reset
and successful reruns, and Linux passed all four checks (66 integration and
5 wire cases). The 55 other internal functions remain outside this phase.
The inventory mapping is frozen before acceptance; its pending labels remain
unchanged. Final bounded implementation acceptance is recorded separately in
`evals/results/rust-phase1-closeout-8b7a6c95.json` and PORT-STATE.md; current
documentation PR-head CI, full suites and final review remain required before ready.

The final judge always executes the seven startup scenarios and six concurrent
call cells in addition to the original nineteen candidate cells. Both scenario
JSON contracts are included in the instrument fingerprint; public bindings
retain the executable identity of all thirty-two cells, while their raw streams
remain private. Startup stderr still has no allowlist. Concurrent call order is
authorized only by the separately captured per-platform final-pair contract.

New parity selects its Python source from immutable workflow event commits:
`pull_request` uses `git merge-base(base.sha, head.sha)`, not the synthetic PR
merge checkout; `push` uses `after`; manual master capture uses `frozen_head`,
which must equal the dispatched checkout, with `GITHUB_REF` required to be
`refs/heads/master`. Both manual `ci` and `wait-mail` modes require `frozen_head`.
The selection JSON records the ref. No extra dispatch input is needed.
Each new receipt records `oracle_head`; source receipts also retain the event
selection and actual source/schema. A stale historical pin is information only
and cannot freeze the port. Source/import binding and behavior comparisons
remain required. Historical receipts, inventories and frozen contracts retain
their recorded SHAs and are never rewritten as current evidence. Active EOF
parity keeps successful silent completion, terminal responses, frame coverage
and the existing final-pair ordering policy. Startup keeps required exit/frame
outcomes, health requests, diagnostic presence and refusal traffic controls;
current Python and Rust stderr bytes remain fully compared. Historical EOF
streams and startup identity templates are retained as informational comparisons.

PR parity intentionally uses the merge-base Python behavior as its baseline.
A Python-only behavior change therefore passes PR parity against the unchanged
Rust port and is first caught by the master push run, which selects the new
Python commit. A PR changing Python behavior and its Rust port together fails
PR parity until its base includes the Python change. This is the intended
ordering for changes to `pseudolife_memory/`.

The workflow binds `PSEUDOLIFE_PORT_ORACLE_SELECTION` before preparing metadata,
running harness tests or creating the isolated oracle. For an explicit local
proof, save the actual event payload and select it first (no moving-master
fallback):

```sh
python -m evals.rust_port.oracle_selection --event-name pull_request --event-path /private/event.json --checkout-head FULL_CHECKOUT_SHA --out /private/selection.json
export PSEUDOLIFE_PORT_ORACLE_SELECTION="$(cat /private/selection.json)"
```

Then prepare an isolated oracle from the checkout-installed dependency runtime:

```sh
python -m evals.rust_port.phase1_oracle --destination /private/new-phase1-oracle
cd /private/new-phase1-oracle/source
export PATH="$PWD/../runtime/bin:$PATH"
../runtime/bin/python -m evals.rust_port.phase1_ci --oracle-root . --candidate-json '["/absolute/current/rust/target/release/pseudolife-stdio"]' --candidate-root /absolute/current --out /private/new-result.json --public-out /absolute/current/evals/results/new-result.json --offline-resource-checked-at VERIFIED_UTC_TIME
```

On Windows the interpreter is `../runtime/Scripts/python.exe`; prepend its
directory to `PATH` so the existing lease CLI resolves from that runtime. Preparation
creates a detached clone at the selected event oracle SHA, overlays
only the current harness instruments, and installs that oracle editable without
dependencies in a private runtime. It checks both source bytes and imported
package/version identity. The candidate is the current absolute binary path;
the immutable test functions execute from the selected source. Dependency paths
come from the caller's installed `[dev,lite]` runtime. The destination must be new.


Historical candidate `2e628b27` receipts are retained at
`evals/results/rust-port-phase1-rust-windows-2e628b27.json` and
`evals/results/rust-port-phase1-rust-linux-2e628b27.json` to reconstruct the
strict-judge evidence associated with the original paired measurements below.
They record zero differences and process cleanup, but lack actual pytest node
outcomes; their binary bindings were added outside the committed instrument.
They are explicitly historical and cannot satisfy final close-out acceptance
or be reused by schema 2. Their recorded source identities remain unchanged.

Keep one current complete combined receipt per replay kind (`python-self` or
`rust-candidate`) and platform (Windows or Linux). Promote four fresh receipts
only after checking their final instrument fingerprints, source/executable
bindings, all eight actual node passes, zero differences and cleanup.
The four current 690bb8ac captures are
`rust-port-phase1-python-windows-690bb8ac.json`,
`rust-port-phase1-python-linux-690bb8ac.json`,
`rust-port-phase1-rust-windows-690bb8ac.json` and
`rust-port-phase1-rust-linux-690bb8ac.json` under `evals/results/`.
They each record eight actual pytest passes, 32 bound candidate cells, zero
retained-wire differences and verified complete private cleanup. The public
files exactly use the committed export behavior, omitting raw stream cells.
Separate `rust-port-phase1-cli-python-{windows,linux}-690bb8ac.json` receipts
record five Python dispatcher passes per OS; they are not Rust CLI coverage.
The eleven other superseded or failed Phase 1 receipts are pruned;
they have no retained-history exception. The two named `2e628b27` files above
are the sole historical exception. The contaminated Windows measurement smoke
is removed; it is not measurement evidence.

Paired measurements of `2e628b27` completed three ten-sample repeats per arm
on each platform, with SDK preflight included and all 120 launches clean.
Receipts are `evals/results/rust-phase1-measurement-windows-2e628b27.json` and
`evals/results/rust-phase1-measurement-linux-2e628b27.json`. Peak RSS is sampled;
executable size excludes runtime/dependency footprint. These two paired
receipts are retained as the named before-retirement measurement history,
with their original source/runtime bindings; they are not current evidence.

Current post-retirement pairs are
`evals/results/rust-phase1-measurement-windows-690bb8ac.json` and
`evals/results/rust-phase1-measurement-linux-690bb8ac.json`, each three repeats
of ten samples per arm with clean normal and separate RSS launches. PORT-STATE.md
records their numeric quantiles and per-arm repeat floors. Historical r5 remains
unchanged and noncomparable; its canonical LF Git blob SHA256 is
`3d29fcba85601c4647208da7448e85245861af16671ec22a33c9393c43fac581`.
Implementation-head suites and integrated CI passed at `8b7a6c95`; the separate
close-out ledger preserves their source chain and actual routing. Current
documentation PR-head suites, CI and final independent review remain required
before #560 is marked ready.

Phase 2 adds `cli_corpus.py` and `cli_dispatch.py` without changing the historical
Phase 0b corpus. The 15 cases cover help aliases/trailing argv and unknown valid
Unicode scalar arguments, including Python quote/control/separator spelling, with
UTF-8 stdout/stderr explicitly selected. Windows CRLF is preserved; other platforms
use LF. The Rust-local attribute keeps the compiled help asset canonical LF in a
fresh `core.autocrlf=true` checkout. The cases capture ordinary exit status and
raw stdout/stderr from the genuine pinned Python 3.11
runtime, self-replay on the same platform, then compare the candidate with no
newline normalization. Three mutations per case prove that both streams and the
exit code are compared. Five immutable dispatcher tests route through the external
plugin via `PSEUDOLIFE_PORT_CLI_JSON`; explicit `--port-cli-json` takes precedence,
and the default remains Python. JUnit outcomes, executable/source hashes and
unchanged dispatcher/conftest hashes bind the receipt. Logs and JUnit stay in the
private `--evidence-directory`; `--out` is a new public-safe receipt path.

Run `evals.rust_port.cli_dispatch` under the prepared pinned interpreter with
`--oracle-root`, `--candidate-root`, `--candidate-json`, `--evidence-directory`,
`--out` and the existing resource-check option. Candidate Rust source must be
committed and clean. The existing Parity job runs this additive lane on both OS.
Receipts record the UTF-8 stream, scalar argv and platform newline boundary.
Locale/default and other output encodings, non-UTF-8 or surrogate argv,
version/runtime identity and all other mode contracts stay deferred;
Acceptance closes only help and the scoped CLI-DISPATCH cases; other modes
remain deferred.

Current local CLI receipts at `779c588c` are
[`rust-port-phase2-cli-windows-779c588c.json`](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-windows-779c588c.json)
and [`rust-port-phase2-cli-linux-native-779c588c.json`](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-linux-native-779c588c.json).
Each has 15 passing self/candidate cases, 45 rejected mutations and five passing
original outcomes for Python and Rust. Native Linux temporary/evidence storage
is used. Both help measurement pairs, numerical repeat floors and exact
source/tree/binary/runtime identities are linked in `rust/PORT-STATE.md`.
The lock implementation uses Rust 1.94 standard file locks; only `fs2` and its
three unused `winapi` packages were pruned, with `libc` retained. Source validation
passed 277 Linux and 296 Windows Rust tests without skips.
[Run 37245992895](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37245992895)
passed all four Rust/Parity jobs for PR head `779c588c`. Both retained CI CLI
receipts verify the same 15 cases, 45 controls and five Python/five Rust outcomes
at actual PR merge checkout `b5f485c9`, whose tree matches the local head.
All nine reported PR checks passed at `779c588c`.
Only the UTF-8/scalar help/unknown-dispatch slice is accepted; current
documentation-head review/checks remain separate.

Historical local CLI receipts at `6e936887` remain unchanged:
[`Windows`](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-windows-6e936887.json) and
[`native Linux`](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-port-phase2-cli-linux-native-6e936887.json).
Their help pairs and executing
[CI run 37242071017](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37242071017)
retain their original identities in `rust/PORT-STATE.md`. The earlier mounted-home
Linux collection failure remains private and is excluded from acceptance.

## Combined version capture scope

The additive `evals.rust_port.lease_headers` lane runs the public candidate
against a disposable loopback HTTP peer and retains genuine pinned Python
captures unchanged before policy comparison. The approved
`http-forbidden-input-refused` substitution requires exact exit 1, empty
stdout, field-only stderr and unchanged pre-state, without lease/release
requests or child launch. The corpus retains the eight DEL/CR/LF/NUL instance
cases, all C0 bytes and folded values in the three approved fields,
bearer-file inputs, and check/list propagation.

The approved `http-reply-not-understood` policy takes priority for malformed
surrogate registration replies, including mixed malformed/forbidden fields:
exit 1, empty stdout, `lease: HTTP_REPLY_NOT_UNDERSTOOD` plus the platform
newline, unchanged pre-state and only the registration request needed to
receive the reply. Successfully parsed forbidden fields still take the
header refusal before incomplete-pair fallback. Selected ordinary non-ASCII
registration cases retain local fallback under `native-lease-diagnostics`,
substituting only the named registration-header reason bytes; every unaffected
stream, exit and state field remains exact. Unaffected ordinary bearer-file
cases compare the full raw response exactly. See the
[producer admission contract](../../rust/PARITY.md#lease-producer-admission-and-native-refusals).

Output/file/request mutation controls accompany every substituted response.
The Parity job runs this separate lane without a database or daemon. Private
credential fixtures use OS-native temporary storage; existing corpus
normalizations and Python source are unchanged. This scoped lane does not
promote CLI-LEASE or establish its deferred action, timing or lifecycle gates.

Frozen `5220b5ee` Windows/Linux CLI cells cover 28 exact byte/state cases and
112 rejected mutations per OS. Thirteen version cases include a Python-
shaped marker and native image with canonical console missing, plus a valid
installed fixture using an actual non-ASCII home. UTF-8 streams and platform
newlines are explicit; locale/default encoding remains deferred. Eight
immutable original CLI nodes pass once/non-skipped per arm, and each OS
retains one native invalid-argv control. These storage-free cells start no
daemon or database and do not establish a real-bank transcript. Exact source,
runtime, helper and image bindings remain attached to `5220b5ee`; a docs/results
carrier does not change the executed head. PORT-STATE.md records counts,
source ratio, numeric tables and remaining hosted/review gates. Version
remains deferred.

`lease_operator_cases.json` is an additive Phase 4 **unrun fixture plan** for
native offline break. Its first whole-command cell uses an existing guarded
disposable PG bank with no matching lease, compares exact raw streams and all
unchanged rows/files, and starts no daemon. Held-row cells then require the
identical `fixed-clock-replay` seam in both arms and exact fences, payload TEXT,
audit hashes, expiry and queue changes. Named embedded refusal and native PG
diagnostics have rejecting controls; the retained container transport compares
its actual argv and inherited streams. Use the existing `cli_process` fixture
and `harness.run_cli` capture, preserving raw observations before policy. This
plan supplies no execution receipt or acceptance claim.

Manual `rust.yml` dispatch with `mode=wait-mail` captures one to three named
Windows wait-mail pairs using the frozen master head's Python oracle and a fresh
native release image; ordinary CI remains the default dispatch mode. Set
`case_list` to comma-separated `wait-mail-long-ring-watermark`,
`wait-mail-bad-ring-reason` or `wait-mail-unicode-delivery` IDs and `frozen_head`
to the full commit ID of the dispatched branch/tag. Start with the long-ring
case after this workflow and selector land. The selector uses whole CLI arms,
disposable homes, existing comparison policies and rejecting output controls,
without a daemon or PostgreSQL. A run-specific artifact retains raw pairs,
source/image build bindings, logs and cleanup; download and hash it before
using it as evidence. Capture does not promote acceptance or establish
installed-image timing. An OS selector is a later increment; this lane runs
only on Windows.
