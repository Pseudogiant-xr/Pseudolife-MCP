# Porting contract

The phase 1 behavioural oracle is Python 0.16.0 at
`0b015f9279a778f996e71ee78510695e5fee7196`, using PostgreSQL schema 53.
Historical phase 0b evidence remains bound to Python 0.15.0 at
`3691f5cb75487d3fda54a6bde6fab35dcf32c681` and its recorded runtime.
This rulebook governs an incremental port at executable and daemon-subsystem
boundaries. A compiling translation does not establish parity.

## Evidence and ownership

Tests decide where implementation and documentation disagree. Where tests are
silent, record the Python behaviour and the coverage gap in `PARITY.md` before
porting it. Existing tests remain unchanged. The maintainer selected an external
pytest plugin on 2026-10-03 to route eligible process-boundary assertions to the
candidate executable; direct calls into Python internals remain oracle tests.

The Python and Rust implementations never own the same disposable bank
concurrently. Each comparison uses equivalent generated fixture data and a
recorded seed. Production credentials, client homes, databases and model services
are excluded from fixture discovery. Use the existing disposable-database guards
before any connection capable of writing or dropping a database.

Ports own complete boundaries: use no Python/Rust function bridge, pyo3 or FFI
between implementations. Keep the Python oracle available through cutover.
Worker output is evidence to verify, not automatic acceptance; only the lead
updates parity rows and phase state.

The experimental shim retains the oracle's Python MCP SDK startup preflight.
It invokes `PSEUDOLIFE_MCP_PYTHON` when selected, otherwise `python` on `PATH`,
and requires the `mcp.server.subscriptions` capability before daemon traffic.
Missing or older SDK environments preserve the oracle's diagnostic and exit 1;
a successful preflight is silent. Rust owns the subsequent stdio protocol and
HTTP lifecycle. Measurements include the preflight process and its memory.
The parity lane supplies the pinned Python 3.11 / MCP 2.1.1 environment.

## Types and serialization

| Python behaviour | Rust contract |
|---|---|
| Absent key, explicit `None`, empty string/list/object | Preserve each distinction at the boundary; do not insert defaults during comparison. |
| Integer | Choose a checked representation after inspecting the field's actual accepted domain; reject narrowing or overflow exactly as the oracle does. Python booleans can be accepted as integers: check the actual boundary rather than its type annotation. |
| Float | Preserve finite values and field semantics; score tolerance is explicit below. NaN and infinity are never silently normalized. |
| Text | Preserve Unicode, normalization, case, encoding and whitespace unless the oracle explicitly transforms them. |
| Mapping | JSON object key order may be ignored by a semantic comparison; arrays retain order and multiplicity. |
| Timestamp, UUID, episode, HLC | Preserve wire shape and ordering semantics. Normalize only fixture-generated nondeterminism at named paths. |
| CLI result | Exit status, stdout and stderr are separate outputs. Preserve help text and newline/encoding behaviour. |

Do not rely on a serializer's defaults to reproduce Python coercion or missing
field behaviour. Test unknown fields, wrong types, nulls, empty inputs and
boundary values against the oracle. Error text may be user-visible contract.

## Errors and recovery

Map each failure at its public boundary: MCP error code/data/message, HTTP status
and body, or CLI exit/stdout/stderr. An internal Rust error enum is not a wire
specification. Preserve distinctions between invalid input, authentication
failure, unavailable principal state, expired identity and transient transport
failure; an ambiguous rejection must not rotate durable identity.

Cancellation, timeout, EOF, reconnect and shutdown have explicit ownership.
Ensure spawned processes, response bodies, tasks and locks are released by their
owner on every exit path. Audit state across every await, including cursor and
high-water updates. Test the next ordinary operation after a failure, not only
the failing request. Do not claim that successful retries erase failed runs.

## Unsafe code and dependencies

Default to safe Rust. Every necessary unsafe operation requires a narrow module,
a written invariant, cross-platform validation where relevant, and independent
review before acceptance. Do not replace OS lock semantics with an in-memory
mutex or a PID-file existence check. Use bounded build parallelism and respect
the shared full-suite queue. Dependency choices remain provisional until their
supported protocol versions and platform behaviour are demonstrated.

The Phase 1 shim uses `#![deny(unsafe_code)]` with narrow module-local exceptions:
`credentials::windows_security` validates opened credential and coordination
state handles and sets protected owner-only access for new private state;
`lifecycle::posix_session` calls `setsid` in the child before execution. Private
state contents must not be written until its handle has the required owner and
ACL. The Windows module must keep descriptor storage alive while inspecting
borrowed owner/ACL pointers, validate buffer bounds, and release each owned
allocation and handle exactly once without closing borrowed handles. Repository
directory identity lookup is read-only and must not change its permissions.
The Unix callback
must use only async-signal-safe operations between fork and exec. These
exceptions preserve the operating-system behavior of the Python oracle; their
presence does not establish parity. Each requires targeted platform evidence
and independent review before acceptance.

`shim/src/board/doorbell_windows.rs` is a further local exception for Windows
subprocess handling. The Windows module exposes safe `QueueProcess::spawn`,
`wait`, and `kill` methods around job assignment, suspended-child adoption and
thread resumption. Successful native handles transfer into `OwnedHandle`; a
separate process-handle clone pins the leader identity through cleanup, and
thread ownership is checked before resumption. Job creation or assignment
refusal preserves a runnable CLI and selects the native `taskkill /T /F /PID`
fallback. Failed suspended adoption terminates and reaps the child before
reporting definite preexecution failure; timeout and cancellation use bounded
cleanup, while successful launcher completion leaves its worker running.
Closing a job does not kill successful workers. The fallback retains the Python
limitation for workers already orphaned without a job. Injected threads and
failure after partial multi-thread resumption remain outside the current
fixture coverage.

`shim/src/board/doorbell_posix.rs` is a local exception for an execve-only
Doorbell command wrapper, registered after `ProcessSession` installs its
`setsid` callback. Before fork, the parent prepares owned NUL-terminated
program, argument and environment strings and their NULL-terminated pointer
tables. The private `PreparedExec` Send/Sync implementations rely on immutable
tables pointing into those owned allocations for the closure's full lifetime.
With Rust 1.94 and pinned libc 0.2.190, the child callback calls `libc::execve`
and captures raw errno on failure;
it must not allocate, lock, format, mutate the environment or run destructors.
It always returns an error after failed execution, preventing execvp's shell
fallback on ENOEXEC. Rust retains the spawn error channel and failed-child
reaping; Tokio and process-wrap retain successful process and group ownership.
This wrapper applies only to Doorbell's resolved executable, ordinary argv0
and inherited environment with explicit changes; it does not support
`env_clear`, argv0 overrides or subsequent callbacks. Targeted Linux validation
passes the native error, ELF/shebang and session-identity assertions;
frozen whole-candidate validation and independent review remain required
before acceptance.

Board label normalization follows the pinned Python 3.11 Unicode 14.0.0
behavior: casefold, then NFC, with category-C inputs rejected. The pinned
`unicode-casefold` crate uses Unicode 9.0.0; a full code-point probe found 129
casefold differences. `shim/src/board/unicode14.rs` records those corrections
and the 701 category-C ranges, with dedicated normalization assertions.
This compatibility table preserves the oracle's label policy; it does not
authorize normalization at other boundaries.

## HTTP and authentication

Preserve constant-time bearer comparison for both byte encodings, fail-closed
token parsing and the existing DNS-rebinding policy. Hooks use the tokenless
Host and Origin header check in `web/api.py:_browser_gate`; it is skipped when
authentication is configured and does not check the peer address. With no
headers present the gate passes. The MCP mount separately applies the SDK's
token-aware transport-security policy: loopback Host/Origin patterns with
rebinding protection when tokenless, protection disabled with configured auth.
Redirect refusal applies to every credential-bearing header, including custom
identity headers. Use explicit timeouts and cancellation; preserve streaming,
long-poll and backpressure semantics. Protocol negotiation must cover the
current MCP revision and every earlier revision the Python implementation
accepts. Tool visibility gates listing, not invocation authorization.

Credentials are never logged, included in committed transcripts or printed in
assertion failures. Test credentials are generated within disposable fixtures.
Record booleans when comparing credentials. Never infer production endpoint or
credential defaults from the caller's installed client configuration.

## SQL and durable state

Read and write the recorded phase-start schema (53 for phases 0b and 1) without
DDL changes, new tables or repurposed columns. Re-pin the oracle to master at
each phase start; any upstream schema bump follows CLAUDE.md's seven-place
checklist and is never made by the port itself. Use bound parameters, explicit transaction ownership and
oracle-equivalent isolation/locking behaviour. Preserve HLC ordering, contender
selection, audit-chain bytes, mail cursors, lease fencing and FIFO queue rules.
Hydration and clean-exit flush are part of the contract, not optional caches.
Either implementation must be able to open a bank written by the other.

## Floating-point and ranking parity

Default comparisons are exact. A caller may opt named numeric score paths into
an explicit absolute/relative tolerance; record the chosen values with every
run. No tolerance applies to IDs, counts, array order or rank order. Any change
in golden-corpus result order fails parity, even when numeric differences are
small. Reject unlisted normalization and non-finite values.

The same-ONNX-graph embedding tolerance is **not yet established**. The current
configuration guide documents torch fallback for the default Qwen model because
it lacks a supplied ONNX artifact. Phase 3 must first produce and identify the
same graph and tokenizer inputs, compare it with the Python oracle, and record
the observed error before accepting a tolerance. No bit-identical or embedding
equivalence claim follows from comparator support alone.

## Phase 1 stdio comparison

The candidate is `pseudolife-stdio` (`pseudolife-stdio.exe` on Windows), built
from the `rust/shim` crate in the `rust/Cargo.toml` workspace. Rust 1.94.0 is
pinned by `rust-toolchain.toml`. The initial targets are
`x86_64-pc-windows-msvc` and `x86_64-unknown-linux-gnu`; no installation path
selects this candidate. The dependency choices and build commands are recorded
in `rust/README.md`.

The named `stdio-raw-compared` policy retains and compares stdout bytes on the
capture platform. It applies `source-text-lf` only to escaped newline token
spans at `/body/result/tools/*/description` and `/body/result/instructions`.
All other whitespace, key ordering and numeric spelling remain exact; the
comparator does not reserialize a parsed object to manufacture byte equality.
The stderr message allowlist is currently empty, so every stderr byte is
compared. Exit codes are exact. This policy does not change the phase 0b
daemon HTTP/MCP `raw-mcp-retained-not-compared` policy.

The named `eof-observed-final-pair-orders` rule admits only the final two connection-closed error
frames in orders actually observed in the frozen Python capture set. Both
orders occurred in five repeats per protocol era; all preceding frames, error
bytes and frame multiplicity remain exact. A pending modern subscription
produced exactly one acknowledgement before its final `-32000` error in all
five repeats. The rule is not a general frame sorter and does not permit extra
acknowledgements. Evidence remains platform-specific.

## Measurement and acceptance

Daemon oracle captures and baselines run on Linux; the current phase 0b receipts
were captured on WSL2, while the production daemon runs in a Linux container.
Shim and CLI captures run on Windows and Linux. Every capture receipt
records its platform; a missing platform is a validation failure. Historical
PR #540 captures retain their original commit and platform and cannot stand in
for the phase 0b pin.

The named `source-text-lf` rule normalizes CRLF and CR to LF on both sides at
`/body/result/tools/*/description`, the source-derived MCP descriptions in the
current corpus. Other parsed docstring or help fields require their own named
paths before this rule applies.
CLI stdout and stderr, including raw CLI help bytes, remain byte-exact on the
capture platform. `source-text-lf` applies to parsed source-derived text fields,
never a silent global normalization of streams. Captured raw MCP text bytes remain beside the
parsed form for later stricter comparison without recapture.

The HTTP header comparison allowlist is `content-type`, `content-length`,
`cache-control`, `location`, `www-authenticate`, `allow`, `retry-after`,
`mcp-protocol-version`, `mcp-session-id` and `x-pl-board`. Header names are
case-insensitive; missing and present headers differ. MCP session IDs may be
fixture-symbolic under a named rule. `content-length` is excluded only where
one side uses fixed framing and the other chunked framing, with the exclusion
recorded in the receipt. The coordination-start hook deliberately sets
`x-pl-board`; transport-generated `date` and `server` are outside this allowlist.

The named `content-length-authorized-wire-spans` rule validates a retained
declared length against the received entity bytes, then adjusts its compared
value only by the byte deltas of disjoint raw tokens changed by authorized
identity, clock or source-text line-ending normalization. Nested JSON escaping is preserved. Raw length,
adjustment and compared length are recorded; bytes outside those tokens still
count. Whole-body reserialization cannot supply the adjustment. This is
normalized length parity, not a claim that the raw headers are identical, and
does not introduce another header exclusion.

Epoch normalization preserves numeric type, sign, unit and magnitude class:
seconds versus milliseconds differ and integer versus float differs. Normalize
only named fixture-generated nondeterminism; never turn all positive epochs
into one sentinel. Duplicate JSON object keys are detected before parsing a
candidate response and are recorded as a difference.
Epoch symbols mask exact values and fractional precision while retaining the
type, unit and magnitude checks above. HLC values become symbols after format
and declared continuity/order checks, so their component values and digit widths
are not compared and no HLC unit or magnitude parity is established. Mailbox
cursors retain their numeric suffix exactly while replacing the validated agent
identity with a symbol; no exact byte parity is established for that replaced
identity.

Every published performance value comes from a script under `evals/` and an
artifact under `evals/results/`. Record the oracle and candidate commits, dirty
state, anonymous host identifier and hardware/software, model identity, fixture
seed, bank size, warmup, repetitions and workload. Measure a repeated identical
control to establish noise. Smaller deltas are not findings. Contended smoke
runs demonstrate plumbing only; they cannot substitute for quiet-host baselines.

Self-validate the differential harness against Python and a deliberately broken
compiled Rust fixture before trusting it. This proves the comparison mechanism,
not parity of any production Rust surface. Preserve request/response evidence
and report precisely which selected tests exercised which implementation.

The phase 0b acceptance receipt is
`evals/results/rust-port-phase0b-acceptance.json`: the 25-case full-bank corpus
passes through an owned Python command and an attested external URL, all seven
graded proxy controls include their expected rejection reason, and the Linux
selfcheck and Windows CLI replay have zero differences. The compiled garbage
Rust fixture is rejected on both platforms. URL candidates need the disposable
bank adapter documented in `evals/rust_port/README.md`; arbitrary production
servers cannot be pointed at a bank. This acceptance covers the selected
synthetic corpora, not every registered surface or a production Rust port.

The phase 3 daemon reference is the Linux (WSL2) 2,000/20,000-entry matrix in
`evals/results/rust-phase0b-daemon-scaling-linux.json`, with three repeats per
bank/thread combination and separate warm/cold arms. Both policies use CPU
fp32; the production-thread policy resolves to four threads on the recorded
host. The 20,000-entry arm raises only flat-band capacity to retain its corpus.
Generated entry vectors measure resident storage and scoring scale, while query
embeddings use the real model. RSS scaling includes indexes, entry objects and
allocator effects; vector tensor bytes are a lower bound. Match these conditions
and each cell's measured noise floor before making a phase 3 comparison.

The baseline's exact shared provenance helper predates the final harness helper.
`evals/results/rust-phase0b-baseline-source-reconstruction.json` binds it to
`evals/results/rust-phase0b-baseline-runtime-provenance-32740c866530c6b4.py`.
Use those preserved bytes to reconstruct the frozen baseline; do not substitute
the current helper or relabel the capture. Hosted CI repeats use the reviewed
PR #540 head `f2ee15241c29e439c9aaad6fd271683a7a065b3e`, independently of the
daemon oracle above. They measure only that fixed CI reference's noise; a
cancelled master repeat supplies no successful control.

Phase completion is governed by `PORT-STATE.md` and `PARITY.md`. No deferred row
counts as complete. Maintainer decisions are required to retire behaviour,
change a public wire format, merge, deploy or mutate a live bank.

## Disposable trial corrections

The private trial covered deterministic HLC transitions, tool-tier normalization
and stepping, and changed-result detection. One implementer and two independent
adversarial reviewers examined the same frozen candidate. Their findings correct
this rulebook; the trial is not retained as a production implementation and
establishes no production parity.

- Separate oracle execution from observation. An exception while packing a
  successful Python result is a harness failure, never an oracle-unit error.
  Require a complete response count and successful process exit before interpreting
  a comparison's mismatch count.
- Separate arithmetic range from decimal conversion policy. Python arithmetic
  can succeed for an integer whose decimal serialization exceeds the runtime's
  configured limit. An envelope must represent its declared input domain without
  globally changing the oracle JSON parser's own acceptance rules. The trial's
  unrestricted-integer coverage claim was not substantiated: packing a successful
  sufficiently large HLC result raised an exception in the observation layer.
- Test bool/int coercion at each boundary. HLC ticks and tier deltas can accept
  booleans where the changed-result detector requires literal boolean identity.
  Rust's stronger types do not authorize rejecting inputs accepted by Python.
- Pin runtime-dependent semantics: Unicode whitespace and lowercase behaviour,
  error-string representations, JSON non-finite constants, surrogates and numeric
  parsing limits need explicit oracle evidence.
- Keep trial exclusions deferred. Pure HLC arithmetic establishes neither
  locking nor atomic updates; the trial also excludes object subclasses, byte
  text, deep recursion and tier warning logs. None is retired by implication.
