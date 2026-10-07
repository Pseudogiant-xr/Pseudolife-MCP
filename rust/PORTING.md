# Porting contract

The current version-branch behavioural oracle target is Python 0.17.0 at
`3c01bb31abd60178e15dea99adda369b4bbf92fc`, using PostgreSQL schema 55. Scoped CLI-VERSION is ported at merged master `df2dbf8a`, tree `28823784`; the [final both-OS CPU proof](../evals/results/rust-phase2d-version-df2dbf8a/README.md) retains the full executed identities. Other CLI modes keep their own gates; this evidence carrier requires its own review and hosted checks.
Historical phase 1 close-out evidence retains Python 0.16.1 at
`f709abb54f7912ae9cd767998d0926ca33df4bcd`, using PostgreSQL schema 54.

Installed-version parity covers manifests written by the runtime installer. Non-standard NaN/Infinity values, lone Unicode surrogates and integers beyond u64 are explicitly deferred; their captured differences are retained separately. This version scope decision applies only to version manifests. Lease daemon replies
have a separate producer admission rule in [PARITY.md](PARITY.md): finite UTF-8
JSON, with malformed/non-finite 200 replies refused before local fallback,
lock creation or child launch. Healthy coordination and suite metadata producers
define that lease input shape; historical synthetic interpreter grammar receipts
remain historical, and successor execution/acceptance is still pending.

The [local version evidence at `62e4f590`](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/2d29b6e5782b5943f472ffaf3b0ebb89d8cab9e5/evals/results/rust-phase2b-version-62e4f590/README.md)
records both-platform release corpora and bare/installed paired measurements.
It preserves the Windows installed median/p95 regression and distinct runtime
versions, with no aggregate speedup or causal claim. Those receipts remain
historical and do not certify a later head; current scoped CLI-VERSION
acceptance is bound to the final `df2dbf8a` proof above.

Historical phase 1 evidence retains Python 0.16.0 at
`0b015f9279a778f996e71ee78510695e5fee7196` and schema 53.
Historical phase 0b evidence remains bound to Python 0.15.0 at
`3691f5cb75487d3fda54a6bde6fab35dcf32c681` and its recorded runtime.
This rulebook governs an incremental port at executable and daemon-subsystem
boundaries. A compiling translation does not establish parity.

## Evidence and ownership

### Episode hook producer grammar

The legacy hook producer supplies an ordinary JSON object with STRING
`session_id` and missing/null/string `cwd`. Typed serde_json validation replaces
Python value coercion, non-finite numbers, integer interpreter options and
pinned recursion-depth behavior. Keep the OS-path/string codec and exact
request-body escaping; synthetic interpreter inputs remain retained historical
diagnostics. The producer and each deliberate substitution are recorded in
PARITY.md. Original oracle/eval tests remain unchanged; only the explicitly
retired native interpreter-limit tests and string-key origin fixture change.
The unused general `python_json*` additions and their module declaration are
removed from the episode candidate; no lease production consumer exists in this
tree. VERSION's helper visibility is restored to the accepted base. Earlier
runtime receipts keep their original images and do not certify this removal.

### Prepared episode policy instances

The header-disposition statement from the original preparation at `758aa642`
is historical; the narrow delegated disposition below supersedes that pending
decision without changing raw receipts or promoting either episode mode.

The episode branch integrates Python master `0b46bb8e2010cf0e428dd650b98cedb165ad3d75`.
Its named policies and additive executable nodes are preparation; final-head
runtime receipts, hosted execution, measurements and independent review are
pending. Episode mode rows remain deferred.

- **`http-forbidden-input-refused`:** field `PSEUDOLIFE_MCP_TOKEN` configured
  bearer; first case `token-del`. Reject DEL (U+007F), C0 U+0000–U+0008 and
  U+000A–U+001F, including CR/LF folding, with exit 1 and named diagnostic
  `HTTP_FORBIDDEN_INPUT_REFUSED`: `[shim] invalid PSEUDOLIFE_MCP_TOKEN: forbidden HTTP header bytes.`
  Stdout stays empty, stderr retains platform newlines, home bytes stay exact
  and no episode POST is sent. Validation stays after accepted stdin, origin,
  successful health and title derivation; earlier silent gates remain earlier.
  Instances: `token-del`, `token-control`, `token-fold`, `bearer-del`,
  `bearer-control`, `bearer-fold`, `token-invalid-line`. The first six are
  three aliased vectors. The seventh contains CRLF despite its historical
  exact result. Expected refusal bytes are explicitly a candidate oracle,
  `ported-with-substitution`, never Python bytes. HTAB, Latin-1/non-Latin-1,
  token-file-only and empty-environment controls keep their existing behavior.
- **`nondeterministic-bytes-semantic`:** decoded POST `/title` minute, first
  case `episode-key-11-episode-start`; second `episode-repair-cwd-12`.
  The exact prefix and ` - YYYY-MM-DD HH:MM` shape, calendar-valid local
  minute and JSON escape spelling remain required. Two raw Python captures
  must first show distinct minutes with exact surrounding observations.
  Each arm needs its own finite ordered numeric invocation window shorter
  than 60 seconds and per-endpoint UTC offsets/local minutes; the title
  belongs to that arm's endpoint minute set. Missing bounds are incomplete.
  Only the 16 minute bytes differ; every other body, header, input, stream,
  exit and file byte stays exact. No random-field policy applies here.
  `episode_policy.py` is an additive checker for this narrowly bound shape;
  historical receipts are not upgraded by adding it. The retained cwd-12
  receipts lack numeric windows and still require recapture.
- **`python-traceback-not-contract`:** no retained episode instance. Any
  future uncaught exception comparison preserves terminal exception text,
  stdout, exit and poststate; only traceback header/frames are deferred.
  Existing invalid-origin diagnostics remain exact.

The separate direct group-12 decision permits `ported-with-substitution`
only for `active-health-head` and `active-health-chunk-size`: reqwest's
header/chunk-framing inactivity behavior remains, with a Phase 5 carryover.
This is not a fourth parity policy. Active/stalled health and POST body
controls remain exact; historical failed controls and private proxy failures
remain failures. No new body-stall allowance is introduced.

`episode_policy_cases.json` enumerates the 57 raw-header-difference case IDs
per OS outside the three policies. At `758aa642`, implicit header presence,
value and casing, including comparator exclusions, remained deferred pending
the disposition below. HTTPS, proxy/trust and
locale-default stdin behavior are also unproved; no broad transport parity
claim follows from the additive loopback fixture nodes.

### Narrow episode wire disposition

Episode timeout capture may request `process_timing` from `observe`/`run_cli`.
Its `communicate_completed_monotonic` value is a subprocess completion upper
bound recorded before ownership cleanup and post-exit snapshots/image hashing.
The existing stalled-body time bound is unchanged; new captures anchor that
bound to this endpoint and retain the historical later capture endpoint as a
diagnostic. This is an instrument correction, not a native body-stall policy.
Equal-image warm episode measurements reopen the owned episode before every
control, warmup and timed invocation, with setup and state readback untimed.

The delegated 2026-10-07 decision permits two explicitly named comparisons
in `episode_wire.py`; it requires Referer absence through implementation.
Both modes remain deferred pending final-head executable proof.

- **`http-field-name-case-insensitive`:** HTTP field-name association at
  `/wire/*/headers`, first retained case `episode-key-6-episode-start`.
  Names are ASCII HTTP tokens, associated by lowercase ASCII spelling;
  every value and presence remains exact except the following named case.
  Any duplicate name or case collision fails closed, even with equal values:
  no overwrite, comma coalescing or whitespace normalization. Ordered-pair
  captures retain raw order. The subsequent delegated disposition permits
  ordering differences among distinct field names, through
  `associated_wire_observations_match`; the existing ordered-pair comparator
  remains a strict diagnostic. Same-name multiplicity and case collisions
  are rejected before any mapping conversion. Request line, terminator,
  body-read bytes, routing, auth, values and other presence remain exact.
  Historical mapping receipts lost duplicate-line
  information before comparison and cannot prove duplicate absence or raw
  header ordering/framing equality.
- **`episode-absent-accept-wildcard`:** only the exact 57 recorded IDs in
  `episode_policy_cases.json`, first `episode-key-6-episode-start`; field
  `/wire/*/headers/accept`. Oracle absence may compare with candidate exactly
  `*/*`. Any other added Accept value, reversed presence difference, changed
  existing value or other added header is rejected. Authentication, Host,
  routing, bodies, framing and every other captured field remain exact.
  This explicit server-visible substitution is not an additional general
  parity policy and does not authorize other case IDs.

Referer is never normalized. The episode health client uses `.referer(false)`
to match urllib's absence on health redirects; the non-redirecting episode
POST client is unchanged. Actual pinned reqwest 0.13.5 source was read:
`async_impl/client.rs` defaults `referer` to true and passes the builder setting
to `TowerRedirectPolicy`; `redirect.rs::on_request` conditionally inserts
Referer. Python 3.11 `HTTPRedirectHandler.redirect_request` copies existing
headers without generating that field. New additive redirect executable
coverage awaits a compiled candidate; historical `health-redirect` still
contains the extra field and remains a mismatch under this comparator.

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

The maintainer's 2026-10-05 phase 2 decision retires the Python MCP SDK
preflight in the Rust candidate (`retired-by-decision`). The checked modules
are Python imports; Rust imports none of them. The Python oracle and its
existing tests retain that guard.

**`no-python-before-first-frame`:** the Rust candidate runs no Python
interpreter before its first complete stdout frame is successfully written
and flushed, except when it must spawn the local daemon. A remote daemon URL
never runs Python. Local fallback spawning requires `PSEUDOLIFE_MCP_PYTHON`
or `PSEUDOLIFE_MCP_SERVE_COMMAND` (a nonempty JSON argv array, preferred when
both are set). Without either, the candidate takes the existing no-spawn wait
and prints the named explanation `NO_CONFIGURED_SPAWN_NOTE`; it never invents
`python` from PATH. This is `ported-with-substitution` for the Python oracle's
`sys.executable` spawn. A configured command is passed as argv, without a shell.
Malformed explicit serve commands print `INVALID_SERVE_COMMAND_NOTE` before
the health probe and use the no-spawn waiting path, even when an interpreter
is also configured. The waiting notes name disabled fallback spawning rather
than claiming the caller set `PSEUDOLIFE_MCP_NO_SPAWN`.

Unattended client updates retain their loopback/no-spawn/newer-version gates
and require the explicit interpreter setting. The writer schedules them once,
after the first successful frame flush; no frame or a failed flush starts no
update. Startup retains the truthful manual version remedy in stderr and
initialize instructions, plus all authentication checks. An update launch
diagnostic appears only after that launch succeeds. An explicit serve command
alone does not identify a Python interpreter for client updates. This changes
the scheduling and initialize version note by the same maintainer decision;
the candidate remains uninstalled by the port. Installer-backed client update
parity is deferred to Phase 5: this candidate proves the explicit-interpreter
launch and scheduling seam against a disposable sentinel module, not a client
installation or an implicit interpreter lookup.

## Briefing and prompt-hook producer substitutions

The experimental hook candidate validates prompt stdin and consumed daemon JSON
payload replies with strict JSON parsing. It consumes string session IDs and markdown while ignoring
ordinary metadata, including integers beyond u64; original Python observations
and historical failures remain evidence rather than candidate expectations.

- **`hook-strict-json-refusal`** governs complete prompt stdin and consumed daemon
  JSON payload replies, including ignored fields. The `/health` availability
  probe is separate: non-JSON health, including HTML 502/503, means no daemon
  and quiet exit 0 before a payload request. Valid non-null JSON health remains
  available, including Python's degraded JSON 503. Prompt-hook performs no
  health probe; malformed memory-change text stays quiet without cursor changes. NaN, Infinity, lone surrogate escapes,
  and malformed JSON are refused.
  Prompt stdin refusal is silent exit 0 before any request or cursor access;
  malformed prompt responses are silent exit 0 without cursor changes or a
  subsequent request. Briefing JSON refusal is exit 1, no stdout and exactly
  `pseudolife-mcp briefing: daemon reply not understood` plus a newline on
  stderr. First case: `prompt-json-nan-extra`. Plain session-start, coordination
  and memory-change response bodies retain their text/cursor grammar.
- **`hook-bounded-json-nesting`** governs JSON resource refusal beyond the
  parser's default depth budget of 128, separately from malformed JSON.
  Prompt refusal is silent exit 0 without a request or cursor change;
  briefing refusal uses the exact named diagnostic and exit 1 above.
  No known hook producer emits deep metadata. Native controls at `64f5a6a2`
  observed 127 nested arrays accepted and 128 refused; a root object plus 126
  arrays was accepted and plus 127 refused. This is the observed serde_json
  default parser budget, not an additional application limit.
  First case: `briefing-json-deep-extra`.
- **`hook-typed-markdown`** governs `/api/briefing`'s consumed `markdown` field.
  Its producer is `MemoryService.session_briefing`, which emits a string.
  False, numbers, arrays and objects use the briefing refusal above; missing,
  null and empty strings remain quiet. First case: `briefing-markdown-false`.
- **`hook-ascii-numeric`** governs briefing caps and COLUMNS. Caps accept signed
  ASCII decimal with ASCII surrounding whitespace and arbitrary magnitude;
  leading zeros/signs are canonicalized, negatives are transmitted unchanged.
  Unicode digits and underscores receive argparse-shaped exit 2. No Python
  digit-limit environment setting applies. COLUMNS accepts positive ASCII
  decimal or falls back to 80; large widths retain finite-text saturation.
  First case: `briefing-custom-negative-unknown`.
- **`hook-first-line-cursor`** governs owned mark reads. Only the first CR/LF
  terminated line is interpreted as ASCII `[0-9.]{1,22}` after whitespace
  trimming; bytes after that line cannot invalidate its cursor. The own writer
  emits the cursor plus LF. First case: `prompt-mark-nonascii-after-lf`.
- **`hook-native-output-failure`** governs hook stdout writes and flushes.
  Briefing/help failure is exit 1 with exactly
  `pseudolife-mcp briefing: output failed` plus a newline on stderr. Prompt
  failure remains silent exit 0 without truncating or advancing its mark.
  Opening a new mark before printing can leave an empty mark. First control:
  `public_help_closed_output_has_native_failure_contract`.

The hook HTTP client uses the oracle-pinned **`Python-urllib/3.11` User-Agent**.
Python's urllib default `Accept-Encoding: identity` and `Connection: close`
are explicitly dropped by the native transport; no full raw-header equality is
claimed. **`http-forbidden-input-refused`** rejects bearer C0 control characters,
DEL and folded values before sending an authenticated payload request. Ordinary
Latin-1 bearer values retain their encoded bytes; other non-Latin-1 values keep
the existing refusal. Raw historical Python bearer diagnostics remain retained.

**`hook-absent-accept-wildcard`** permits only absent-oracle Accept versus native
exactly `*/*` for the 53 recorded hook IDs enumerated in PARITY.md. No other
Accept difference or unrecorded ID receives this disposition. **`http-field-name-case-insensitive`** associates HTTP field names
by lowercase ASCII spelling; distinct-name ordering is free, while same-name value
order, multiplicity and bytes stay exact. The port-owned comparison helper
retains raw observations and provides no other header, body or framing waiver.
The native client disables Referer generation on redirects, including `/health`;
Referer is never removed by comparison. The targeted native controls exercise
health redirects, degraded health and malformed payload separately.

All surrounding authentication, path, cursor and hook-envelope wire semantics
remain required. The hook serializer retains ensure_ascii escaping, including
astral surrogate pairs; this strict parser is local to hooks and changes no
separate lease parser. Native policy controls retain the raw oracle function
and label candidate substitutions explicitly.

## Types and serialization

The SQL-first maintainer sent candidate is pinned separately to Python
`0b46bb8e2010cf0e428dd650b98cedb165ad3d75`. For this leaf, status, the four
application headers and body bytes are exact: no object sorting, rounding,
Unicode normalization, timestamp substitution or semantic JSON comparison.
Database-observed JSONB member order is retained. SQL orders by descending
`created_at`, then ascending `message_id`; expired/read/acknowledged/repudiated
messages are not filtered. UUID and datetime `default=str` cases are named
supplemental serializer producers, not claims about the current SQL columns.

The maintainer approved `config-yaml-typed`: use the maintained pure Rust
`yaml-rust2 = 0.13.0`, without its encoding feature, instead of the deprecated
serde_yaml stack or a bespoke YAML parser. Its presentation events retain
quoting and tags before the safe loader constructs values. The audited fields
are `coordination.enabled`, `coordination.allowed_principals`, and
`coordination.maintainer.rp_id/origin`; missing fields/files use AppConfig's
defaults. Explicit tags are outside this candidate's configuration domain.
Duplicate keys, wrong audited field/section types, multiple documents and
unquoted YAML 1.1/1.2 disagreements fail startup with `config-yaml-typed`.
The disagreement corpus includes yes/no/on/off, leading-zero/0o/0b integers,
base-60 numbers, underscored numbers, differing exponent forms and timestamps;
quoted equivalents remain strings. Plain integer spellings that the YAML
loader would turn into strings are explicitly refused, so they cannot satisfy
an audited string field. Unused ordinary sections retain the
Python loader's ignored-field behavior. The first disposable smoke cell used
JSON-as-YAML and is not evidence for this later YAML reader. The
[upstream parser API](https://docs.rs/yaml-rust2/0.13.0/yaml_rust2/parser/index.html)
documents the event/style information this policy uses.

For eligible original assertions, `evals/rust_port/pytest_plugin.py` passes
the unchanged ASGI request through `sent_http.py` to a real owned native HTTP
process on a generated disposable bank. It never replaces a Python service
method with Rust. Direct Python internal/Recorder assertions remain oracle
tests. Additional paired HTTP/SQL tests own equivalent generated banks
sequentially and check unchanged durable state after each arm. No production
shadow is enabled: Python remains the serving implementation until the full
comparison, snapshot/export, TLS and rollback gates in the accepted decision
brief pass. Embedding-ranked reads remain deferred with no numerical claim.

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

### `nondeterministic-bytes-semantic`: generated registration name

Fields governed: `/body/name` in `register-sender` and `register-recipient` only; `/body/name_source`, `/body/label` and the raw `/body/agent_id` gate the rule. First case: `register-sender`.

Before normalization, require `name_source == ""`, a nonempty string `label`, canonical lowercase hexadecimal `agent_id` matching `[0-9a-f]{32}`, and `name == label + " " + raw_agent_id[:8]`. Replace only those eight generated characters with the actor's named prefix token; label, separator, name source and every surrounding field stay exact. Explicit names and other operations receive no name normalization. Earlier response shapes without both naming fields remain exact; partial naming shapes fail.

Wire-length adjustment touches only the original escaped span of those eight decoded characters, preserving the prefix's Unicode escapes, quoting, whitespace and all surrounding JSON bytes. Per-arm `policy_instances` retain case, arm, policy, raw name, raw label, raw name source, raw ID prefix and normalized name; no credential body is retained. The Phase 1 public CI receipt exports these instances under `generic_controls`.

Instances: `register-sender /body/name`; `register-recipient /body/name`, across each fresh generic arm. Historical hosted runs 37428908416 (#602) and 37429259333 (#603) each show these two cases in arms 1 and 2; their downloaded public receipts contain no raw names, so they prove failure locations, not the raw semantic relation. Source `_board_name` / `_public` and synthetic rejection controls establish the rule; next execution must supply retained raw instances.

## Errors and recovery

### Wait-mail inputs and named policy instances

The wait-mail candidate is integrated with accepted master
`0b46bb8e2010cf0e428dd650b98cedb165ad3d75`; its next full corpus must use
that oracle pin. This preparation does not promote historical receipts.
The adapter writes UTF-8 digest text with an ASCII decimal watermark, an
ASCII ring decision/reason and decimal `.seen` content. The command's public
argv, environment and explicitly selected digest path are user inputs.
The declared producer substitutions in [PARITY](PARITY.md#wait-mail-producer-substitutions)
narrow marker and numeric grammar, help wrapping and stdout failure behavior;
opaque identity/path bytes and every field outside those declarations stay exact.
This local-file mode makes no HTTP requests, so it has no
`http-forbidden-input-refused` or header/chunk inactivity instances.

`nondeterministic-bytes-semantic` governs only delivery stderr's `HH:MM:SS`
and `ledger.log` column 1. First case: `wait-mail-large-watermark`.
The local clock must have the exact format and represent a second in that
arm's own invocation wall-time window; the ledger epoch must be canonical
ASCII decimal in the same window. Every surrounding stderr byte, elapsed
wording, ledger column, stdout byte, `.seen` byte and other state stays exact.
`cli_process.observe` now records the window immediately around the child
invocation; `wait_mail_policy.delivery_projection` validates and records
these two fields for one fresh delivery without rewriting raw observations.

| Case id | Named fields / disposition |
|---|---|
| `wait-mail-large-watermark` | Windows delivery clock and ledger epoch; historical raw failure retained. |
| `wait-mail-non-ascii-space-body` | Linux delivery clock and ledger epoch; historical raw failure retained. |
| `wait-mail-delayed-ring` | Linux delivery clock and ledger epoch; historical raw failure retained. |
| `wait-mail-delayed-digest` | Linux delivery clock and ledger epoch; historical raw failure retained. |
| `wait-mail-cr-spaces-ring` | Both hosts' earlier clock-only failures retained. |
| `unicode-session` | Earlier Windows clock instance; no same-id second-Python claim. |
| `wait-mail-unicode-delivery` | Both hosts' positive delivery; additive bounded-clock node prepared, final-head execution pending. |
| `seen-directory` | Both hosts' one quoted source basename `.tmp-[a-z0-9_]{8}.seen`; exact directory, destination and error bytes; any clocks governed separately. |
| `invalid-session-bytes` | Linux `python-traceback-not-contract`: only traceback header/frames deferred. |
| `inline-session-bytes` | Linux `python-traceback-not-contract`: only traceback header/frames deferred. |
| `environment-session-bytes` | Linux `python-traceback-not-contract`: only traceback header/frames deferred. |
| `invalid-multiple-session-bytes` | Linux `python-traceback-not-contract`: only traceback header/frames deferred. |
| `inline-unicode-session-bytes` | Linux `python-traceback-not-contract`: only traceback header/frames deferred. |

For the five session cases, exit 1, stdout, the terminal
`UnicodeEncodeError: message` line including its LF and complete post-state
remain exact. The process comparator applies these policies only to the named
case IDs and records each arm's admitted fields; unlisted cases stay exact.
New admission/rejection tests check that path, state, exit and terminal-line
mutations fail. Historical-input replay under this checker remains separate
from final native execution.
The native CI lane runs the unchanged deterministic additive wait-mail tests,
a new actual positive-delivery node and the Python/native help drift guard
at `COLUMNS=80`. It cannot substitute for the complete both-OS corpus,
positive paired measurements, current-head CI or independent review.

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

`tokio-tungstenite` and its transitive `webpki-roots` are optional behind the
default-on `codex-delivery` feature; disabling it retains pull coordination and
native doorbell paths. The existing TLS feature selection is unchanged.

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

`credentials::unix_accounts::{home_by_name, home_with_lookup}` are local
exceptions for reentrant `libc::getpwnam_r` account-database lookup after removing
`users` 0.11. The caller owns the initialized passwd structure and lookup buffer.
Only zero status with a result identifying that structure is admitted; outside
Android, a null home field is rejected before reading its NUL-terminated bytes.
The home bytes are copied before the buffer is dropped, preserving non-UTF8
paths. Lookup starts with 2048 bytes and doubles on ERANGE with checked size
overflow and no added cap. Missing names, interior-NUL names and lookup errors
return `None`. Android retains the prior `/var/empty` home default after an
admitted lookup. Board ownership uses `rustix::process::getuid` for real-UID
checks. These are OS primitives, not a pure-standard-library replacement.

The [isolated comparison](../evals/rust_port/users_comparison/README.md) uses the
actual locked `users` 0.11.0 crate and imports the production resolver. Four
original seam tests remain unchanged; two additive tests reject a populated
error result and a null home field. These defensive rejections are explicit
and are not evidence of former-crate behavior on malformed results. Historical
validation at `43e05ac1` predates this repair. Targeted platform validation and
independent review remain required; Android, other Unix targets, unusual NSS
backends and real/effective-UID divergence are not established by Linux proofs.

`lifecycle::executable::is_executable` is a local Windows exception after removing
`which` 8.0.6. Its owned NUL-terminated UTF-16 pathname and initialized output
buffer remain live through `GetBinaryTypeW`; no native handle is created.
Windows retains symlink metadata and extension-bearing file acceptance, while
extensionless paths require that binary-type check. Unix uses `rustix::fs::access`
with real IDs and ACLs after following metadata to a regular file. Ordered PATH
search, captured cwd and Windows filename casing are compared against the actual
registry crate by `evals/rust_port/which_comparison.py`.

Windows deliberately reads PATHEXT on each lookup. This matches the removed
public `which::which` calls in 8.0.6: their borrowed `RealSys` uses the trait's
default extension parser. It differs from an owned `WhichConfig<RealSys>`,
whose `RealSys` implementation caches the first extension list in `OnceLock`.
The comparison exercises both actual crate APIs after changing `.CMD` to
`.EXE` in a disposable child; the owned configuration still accepts a `.CMD`
file while the public API and replacement reject it. The replacement does not
adopt that owned-configuration cache. This is an explicit lookup policy, not
an assertion that every `which` API has identical behavior.
The maintainer authorized exactly the two fixture resolver-call substitutions;
their assertions and error handling remain unchanged. The
[actual-crate receipts](../evals/results/rust-which-8.0.6-comparison/README.md)
record 23 equal public-API cases on Windows and 17 on Linux, plus the expected
Windows owned-configuration difference. Windows file-symlink coverage remains
unavailable (creation error 1314). These bounded comparisons do not establish
equivalence for every platform, ACL or error message. Current hosted checks
and independent review remain required before acceptance.

`shim/src/board/doorbell_windows.rs` is a further local exception for Windows
subprocess handling. Unsafe allowances are confined to `spawn_phases`,
`adopt`, `kill`, `QueueProcess::drop`, `create_job` and `resume_threads`;
other functions inherit the crate denial. The Windows module exposes safe `QueueProcess::spawn`,
`wait`, and `kill` methods around job assignment, suspended-child adoption and
thread resumption. Successful native handles transfer into `OwnedHandle`; a
separate process-handle clone pins the leader identity through cleanup, and
thread ownership is checked before resumption. Job creation or assignment
refusal preserves a runnable CLI and selects the native `taskkill /T /F /PID`
fallback. Failed suspended adoption terminates and reaps the child. It reports
definite preexecution failure only when no thread has resumed; after any
successful resume, a later failure retains the delivery reservation because
user code may have executed. Timeout and cancellation use bounded cleanup,
while successful launcher completion leaves its worker running.
Closing a job does not kill successful workers. The fallback retains the Python
limitation for workers already orphaned without a job. The partial-resume
fixture injects a failure after a successful native resume and checks
reservation retention and cleanup. Injected threads and an actual operating
system failure on a later thread resume remain outside fixture coverage.

`shim/src/board/doorbell_posix.rs` is a local exception for an execve-only
Doorbell command wrapper, registered after `ProcessSession` installs its
`setsid` callback. Unsafe allowances are confined to `ExecveOnly::pre_spawn`,
`PreparedExec::exec` and its necessary Send/Sync impl items; other functions
inherit the crate denial. Before fork, the parent prepares owned NUL-terminated
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
Wake-reason sanitization also uses the pinned Unicode 14 predicates: categories
L* or N* for `str.isalnum`, and the Python whitespace set including U+001C–001F
for `str.isspace`. The same file retains 733 alphanumeric ranges; the existing
casefold and category-C tables stay intact. The full-range SHA256 contract hashes
one alphanumeric byte followed by one whitespace byte for every code point,
including surrogates. `evals/rust_port/unicode14_reason.py` computes it with the
pinned CPython 3.11 runtime. Sanitization still compacts, limits to 60 characters,
filters punctuation and falls back to `unknown`; the separate `reason14.rs`
asset remains deleted.

The original shim/channel CLI keeps its valid-argument dispatch. OS arguments
are read without Unicode conversion panics; an invalid-Unicode mode follows
Python's unknown-mode repr and exit 2, using surrogateescape on Unix and unpaired
UTF-16 surrogate escapes on Windows.

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

Read and write the recorded phase-start schema (55 at the current Phase 1
pin; historical close-out evidence keeps schema 54) without
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
it lacks a supplied ONNX artifact. The [CPU prerequisite receipt](https://github.com/Pseudogiant-xr/Pseudolife-MCP/blob/13dbe0b032527bf110acbaf4f1ea1ae00ca20cef/evals/results/rust-onnx-cpu-prerequisite-20d0e75d.json)
identifies a disposable fp32 graph and compares Torch fp32 with direct ORT CPU
over 1,000 seeded documents and 25 **PROPOSED** topic queries: maximum absolute
embedding difference `5.401670932769775e-7`, minimum cosine
`0.9999999999933409`, identical full stored-vector rankings for 5/25 queries and
identical top-eight rankings for 25/25. Proposed, unaccepted numerical bounds
for this recorded graph/runtime/corpus are max absolute `6e-7` and minimum
cosine `0.999999999993`, rounding the observed absolute error upward to the
next `1e-7` and cosine loss upward to the next `1e-12`; no ID or ranking tolerance
is proposed. The stock SentenceTransformers ONNX wrapper fails on missing
`position_ids`, so this direct-input proof does not establish shipped-backend
parity. Optimum's hidden-state validation warned that `6.67572021484375e-5`
exceeded `1e-5`. The query proposal and packaging/docs decision require the
maintainer before any tolerance is accepted; no bit identity or speed claim
follows from this receipt.

## Phase 1 stdio comparison

The candidate is `pseudolife-stdio` (`pseudolife-stdio.exe` on Windows), built
from the `rust/shim` crate in the `rust/Cargo.toml` workspace. Rust 1.94.0 is
pinned by `rust-toolchain.toml`. The initial targets are
`x86_64-pc-windows-msvc` and `x86_64-unknown-linux-gnu`; no installation path
selects this candidate. The dependency choices and build commands are recorded
in `rust/README.md`.

File locks use Rust 1.94's standard `File` methods instead of `fs2`, preserving exclusive contention and release behavior while removing the redundant dependency.

The named `stdio-raw-compared` policy retains and compares stdout bytes on the
capture platform. It applies `source-text-lf` only to escaped newline token
spans at `/body/result/tools/*/description` and `/body/result/instructions`.
All other whitespace, key ordering and numeric spelling remain exact; the
comparator does not reserialize a parsed object to manufacture byte equality.
The stderr message allowlist is currently empty, so every stderr byte is
compared. Exit codes are exact. This policy does not change the phase 0b
daemon HTTP/MCP `raw-mcp-retained-not-compared` policy.

The named `cli-state-compared` policy compares ordinary exit status, raw UTF-8
stdout/stderr and the presence and bytes of every post-state file under the
isolated CLI home. Both arms reuse the same home path after a reset; paths and
newlines remain exact. The initial corpus admits no normalizations. Future
PID, timestamp or port normalization requires a named rule at specific file
and byte spans here before a case may opt in. Locale output including cp1252
remains deferred. Candidate-output controls mutate the captured candidate
exit, each stream and a post-state file; oracle self-record mutations are
reported separately as judge-sensitivity controls.

The named `eof-observed-final-pair-orders` rule admits only the final two connection-closed error
frames in orders actually observed in the frozen Python capture set. Both
orders occurred in five repeats per protocol era; all preceding frames, error
bytes and frame multiplicity remain exact. A pending modern subscription
produced exactly one acknowledgement before its final `-32000` error in all
five repeats. The rule is not a general frame sorter and does not permit extra
acknowledgements. Evidence remains platform-specific.

The named `non-eof-observed-final-call-pair-orders` rule covers two public
calls whose upstream requests both arrive before any held response releases.
Their complete response frames must arrive while stdin remains open. Separate
A-then-B and B-then-A releases require those exact orders; one shared release
admits only the two final call-frame orders observed in the platform-specific
Python captures. Earlier frames, exact response bytes, IDs and multiplicity
remain fixed. `stdio_concurrent_orders.json` records the capture bindings; this
rule does not change EOF ordering or permit arbitrary frame sorting.

`stdio_startup_contract.json` records seven startup stderr contracts from the
pinned Python source and raw platform captures. Expected bytes substitute only
the exact fixture URL and credential-file path and generate the platform text
newline; captured streams are never rewritten. Refusals require exit 1, empty
stdout and no MCP POST traffic. The manual version note uses a fixture without
unattended-update capability and is separate from the approved post-first-frame
updater behavior. `phase1.py` invokes `stdio_scenarios.py` in every final judge;
its seven startup and six concurrent candidate cells require executable bindings.

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

## Phase 2 CLI contract

Phase 2 CLI dispatch reads only the first argument and keeps the binary named
`pseudolife-stdio`; the current proof and help measurement select UTF-8 stdout/stderr.
Help bytes are the literal pinned `cli.py::_USAGE`, kept LF by the Rust-local
checkout attribute, with Python's Windows CRLF translation preserved and LF
elsewhere. Unknown valid Unicode scalar
argv uses CPython 3.11/Unicode 14 category-C plus separator repr rules, sharing the
existing pinned table. Locale/default and other output encodings and
non-UTF-8/surrogate argv remain deferred. Recognized modes
without an implementation emit a candidate-only deferred diagnostic, never the
Python unknown-mode contract. The scoped ported version implementation derives the
runtime root from its own executable under a six-digit `runtimes` entry's
`Scripts` or `bin` directory, matching Python's `sys.prefix` identity. It checks
its own executable, also requires the canonical Python console path to be a file
(`Scripts/pseudolife-mcp.exe` or `bin/pseudolife-mcp`), and matches the runtime
path as written or canonicalized. The marker follows Python's dictionary shape.
Manifest NaN, Infinity, lone surrogates and integers beyond u64 remain deferred.
The three original version nodes are routed among eight unchanged CLI nodes per
arm. Final `df2dbf8a` Windows/Linux CLI cells cover named-console admission,
missing-console fallback and actual non-ASCII homes with UTF-8 streams; all four
3x10 warm pairs retain exact output/state and warmed file identity. Counts,
per-arm floors and limits are in PORT-STATE.md and the final packet. The
candidate is not installed; this evidence carrier requires independent review
and hosted checks, with no recapture of the unchanged executed tree.
The additive CLI corpus retains raw argv/exit/stdout/stderr and uses no output
normalization; CLI cold-start-to-exit is a distinct metric from shim first-frame
and initialize-return timing, using the same paired ordering and repeat floors.

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

## Historical Phase 1 close-out evidence

Frozen candidate `690bb8ac` has current Windows/Linux schema-2 Python self-replay
and Rust receipts, each with eight actual stdio outcomes and 32 executable-bound
cells. Five dispatcher nodes per OS remain separately observed Python CLI tests.
Public receipt exports use the committed allowlist; raw streams remain private.
Source-tree and executable hashes establish source identity and executable
identity, not a build attestation. The paired measurements use three repeats
of ten samples per arm, with separately sampled RSS and per-arm quantile floors.
Current numeric tables and remaining acceptance gates are in PORT-STATE.md;
full suites, final integrated-head CI and whole-change review remain pending.

## Version warm measurement condition

Frozen `5220b5ee` captures retain 28 exact cases and 112 mutation controls on
each OS. UTF-8 output is selected; locale/default encoding remains deferred.
Version measurement makes one untimed start per arm before each of three
ten-pair blocks, then reuses exact executable file identities and restored
state. Each layout retains two byte controls, six warm starts and sixty timed
starts with per-arm floors. Help can explicitly opt in with `--warm-images`;
other CLI benchmark reset semantics are unchanged. Cold-copy Windows results
remain historical, with no security-provider cause established. Current
numbers, identities and remaining hosted/review gates are in PORT-STATE.md;
these CPU CLI cells establish no full-suite acceptance.

## Lease diagnostic boundaries

The phase 2c decisions replace the earlier two-case traceback limitation with
these three named policies. The lease instances are listed individually in
PARITY.md and `lease-policy-cases.json`; retained captures remain historical.
CLI-LEASE stays deferred until the version base is ported, the candidate has
its final-head proof on both platforms, and its remaining acceptance gates pass.

**`python-traceback-not-contract`** governs only the Python traceback header
and frames when an uncaught exception exits the oracle. Exit status, stdout,
the terminal `ExceptionType: message` line including its platform newline,
all other stderr and every post-state byte remain exact. Its first lease case
is `approved-timeout-overflow-traceback`; the same rule admits the retained
closed-list, surrogate-name list and C-int-year list instances. An earlier
candidate refusal with different terminal bytes fails. The preparation checker
requires one validated header/frame span and retains diagnostics outside it.

**`nondeterministic-bytes-semantic`** governs only explicitly named fields
whose Python bytes differ between two retained oracle invocations. Its first
lease case is `lease-run-existing-successful-child`, governing only `ran.json`
field `/t`: a finite unsigned decimal float in the exact byte layout
`{"held": "suite,gpu", "t": <decimal>, "credential": false}`. Each captured
timestamp must satisfy its own inclusive `start_unix <= t <= end_unix` window,
including the second oracle invocation; every surrounding byte, response field
and other file remains exact. Raw captures are retained. Missing windows or a
missing oracle repeat make evidence incomplete and require recapture; aggregate
receipt timestamps cannot supply those bounds retroactively. This lease ledger
admits no random suffix or derived-name field.

**`http-forbidden-input-refused`** governs forbidden header bytes only in
`agent_id`, `credential` or configured `bearer`: C0 U+0000–U+001F, U+007F and
folded values. Its first lease case is `D3-DEL-agent_id`. The intentional
candidate substitution returns exit 1, empty stdout and exactly one stderr line,
`lease: HTTP_FORBIDDEN_INPUT_REFUSED: invalid <field> header`, using the platform
newline and one of those three field names. The diagnostic never includes the
value. The candidate preserves pre-state and starts no child, lease or release
request; a registration needed to obtain the offending reply may already have
occurred. Candidate-arm expected bytes are labelled as substitution evidence;
the Python arm stays raw. Non-ASCII/surrogate inputs keep their existing exact
oracle contracts; this policy does not authorize other fields or transports.

`evals/rust_port/lease_policy_preparation.py` is an offline preparation checker,
with additive static tests. Its exact refusal expectation is also used by the
separate executable `lease_headers` lane, which retains the raw Python arm and
labels candidate substitution evidence. The existing judge, corpus and
measurement instrument remain unchanged; the offline checker alone does not
establish native executable coverage.

Lease Unix-second display follows the scoped Chrono local-zone substitution in
PARITY.md: supported daemon timestamps retain ordinary local time and date
formatting, fractional seconds floor, and unsupported calendar values display
`?`. JSON numeric values remain unchanged. POSIX supported `TZ` settings belong
to the platform local-zone contract; Windows CRT-only overrides do not. This
supersedes the two historical C-int-year list exception expectations without
discarding their raw failure evidence or changing the other named policies.

## Final lease core comparison boundary

The final7e [bounded evidence](../evals/results/rust-phase2d-lease-7e6927b7/README.md)
retains raw responses before policy, actual image/source/runtime bindings and
cleanup. Successful-child comparison admits only `ran.json /t`: each finite
decimal lies within its own recorded invocation window, two Python captures
have distinct clocks, and every other response/file byte stays exact. Nonzero
children have no clock exception. Explicit Windows unlock releases exactly
the acquired first-byte range before closing, best effort; POSIX behavior is
unchanged. Its new test separately reacquires byte zero while the original
handle remains open. Windows CTRL_C_EVENT evidence separately checks child
absence before helper cleanup and actual board/local-lock release.

The named `python-traceback-not-contract` rule additionally admits **only**
`list-expected-extreme` and `list-waiter-extreme` text outcomes: validated
Python OverflowError exit 1 may become native exit 0 with complete independently
specified `?` fixture output, empty stderr and exact unchanged state. Ordinary
Python exit-0 outcomes stay raw-exact; both timestamp JSON companions and all
other list exits stay exact. This does not waive exit status for the other
traceback cells. `extreme_list_response` and the new additive rejecting test
enforce this narrow boundary; terminal/header/frame validation remains the
existing `traceback_response` contract. PARITY records both complete receipt
hashes and the raw Linux 1/0 pair rather than concealing that mismatch.

Only check/list/run are implemented in this candidate. `hold`, `break`,
`delegate` and `designate` refuse exit 1 with `deferred in this candidate`;
the candidate loses those actions until Phase 4. Final hosted acceptance and
independent review remain pending, so neither CLI-LEASE-core nor full mode
is marked ported. Date/TZ/UTF-8/JSON and traceback scope above remains in force.


## Phase 4 lease hold preparation

The new branch prepares native hold and the full action parser/help surface
against the pending Phase 2d base. Hold follows an external PID without
signalling it; its asynchronous mirror preserves local ownership under the
named [hold-mirror-best-effort rule](PARITY.md#phase-4-lease-hold-preparation).
The historical hold checkpoint retained break/delegate/designate exit-1
deferrals. Subsequent operator preparation supersedes those implementation
deferrals; acceptance of all four actions remains deferred.
At that checkpoint, no Phase 4 execution, hosted acceptance or new timing
claim was made; previous runtime receipts retain their original identities.
Windows format/Clippy/targeted cargo tests and box/hosted execution gates remain
pending. Neither scoped core nor full CLI-LEASE is promoted by source preparation.

## Shared PostgreSQL client preparation

`shim/src/pg/` uses tokio-postgres 0.7.18 and tokio-postgres-rustls 0.14.0
with the existing Rustls 0.23.45/AWS-LC stack. This avoids a second OpenSSL
client stack, an ORM and generated queries. The dedicated `Session` has a
10-second connection timeout, sets `lock_timeout` to five seconds and selects
the public search path. Callers borrow its client for queries and transactions
and must observe commit errors; there is no pool or automatic reconnect. Closing
sends PostgreSQL Terminate; cancellation drops only this connection.

The DSN grammar is pinned to the single TCP-host URIs written by
`ops/install-autostart.ps1`, both compose files and the DSN examples in
`docs/guide/configuration.md`, plus libpq keyword quoting for those same fields.
Only host, port, user, password, dbname, sslmode and sslrootcert are admitted.
Unknown configured options and unsupported modes refuse by option name with
exit 1. Diagnostics omit DSNs, credentials, file paths and driver messages.

`disable` sends no SSLRequest; `prefer` follows the accepted negotiation rule
below, retaining root checks on its TLS attempt and allowing one fresh plaintext
retry. Every strict mode requires `S` and a successful TLS handshake. Invalid
SSLRequest responses refuse before startup/authentication. `prefer` and `require`
without roots use encryption without CA authentication. An explicit root file,
or the platform default `root.crt` when no explicit file is selected, enables CA
verification, including libpq's default-root upgrade for `require`. The custom
`verify-ca` verifier checks chain, validity and server-auth usage while omitting
hostname verification. TLS 1.2/1.3 handshake signatures remain verified in every
encrypted mode. `verify-full` uses Rustls chain and hostname verification.

The named `pg-tls-webpki-hostnames` rule restricts verify-full to matching SAN
`dNSName`/`iPAddress`. The native refusal identifies the rule and explains that
libpq accepts matching legacy CN-only/IP-in-dNSName certificates. A disposable
libpq 18 comparison retains both raw mismatches; they are intentional under this
rule. No bespoke hostname verifier is added and `verify-ca` is unchanged. The
`pg_client` fixture checks SAN-based positive/negative cases, both named legacy
refusals, default roots, ambient refusals and graceful closure. It does not prove
real database transactions.

The named `pg-dsn-explicit-tls` scope refuses ambient client certificate/key and
CRL files, PGSSL*/PGTLS* variables and libpq connection/session PG* controls by
name. Only the platform default root file participates implicitly. The pure
resolution module retains configured DSN, existing embedded data, then container
order, and pins the container's own `python -m pseudolife_memory.cli` argv.
At the shared-client checkpoint, embedded start/stop, container process dispatch
and native operator SQL were unimplemented. Fixed-clock operator comparisons
remain a later gate.

Windows transport tests use only owned loopback fake PostgreSQL peers and
disposable fixture CA files; no bank, daemon or trust store is changed. Linux,
real-server TLS, full transaction/audit/state comparison, independent review and
hosted acceptance remain pending. The Phase 2d and hold checkpoint receipts
retain their original runtime identities.

## Offline break source preparation

The [break preparation](PARITY.md#phase-4-offline-break-source-preparation) adds
external-DSN transactions and the existing container-host deployment transport.
The shared client's reviewed TLS policy is unchanged. The embedded lifecycle
has the named `phase4-embedded-pg-deferred` refusal, and generic failures use
`native-pg-diagnostics` without driver details or copied interpreter classes.
Regex 1.13.1 implements the producer's existing secret-shaped-text business
guard; unsupported look-around is expressed as explicit ASCII boundary and
tokenize/tokenise checks, preserving match iteration. This dependency does not
introduce a Python parser or runtime bridge. Audit payloads retain sorted UTF-8
JSON and timestamp decimal spelling because both are hashed durable state.

Only locked offline dependency metadata and source formatting were checked;
compilation and every new runtime cell remain unrun. The additive fixture plan
starts with `lease break fixture-missing` on an owned disposable bank, whose
missing-row result creates no audit/time fields. Subsequent mutations require
the identical clock seam in both arms, all-row/byte comparison and rejecting
controls. Delegate/designate, container cancellation, ambiguous COMMIT and full
mode acceptance remain pending; prior transport measurements are historical.

## Maintainer sent JSONB read domain

The native sent leaf uses **bounded-jsonb-depth**: at most 2048 nested JSONB
containers per proof or wake column. A presentation scan counts only brackets
outside strings before allocation; exceeding the bound yields the existing
private 503 class. serde_json's recursion override is paired with pinned
serde_stacker 0.1.14; a pinned stacker 0.1.25 stack covers decoding, conversion,
encoding and recursive drop, including error paths. Depth 200 must serve;
the boundary controls separately report policy outcomes, not exact emulation
of Python's stack-dependent recursion limit.

**jsonb-no-digit-limit** preserves arbitrary precision integers without a
4,300-digit read cap. Direct SQL injection above Python's decoder limit is a
named policy control, not exact response parity. The canonical Python send
body decoder and board write path are the producer controls; their exceptions,
admission and rollback must be observed rather than inferred from raw SQL.
Unchanged ordinary SQL/ordered-ID responses still require exact HTTP bytes.

Typed config uses pinned pure-Rust regex-lite 0.1.9 for the anchored PyYAML
6.0.3 scalar resolver predicates. yaml-rust2 owns parsing and construction;
the predicates identify resolver disagreement, not a replacement Python YAML
loader. Valid digit-prefixed strings remain strings, quoted values retain
their meaning, and audited allowed-principal names strip, lowercase, deduplicate
in first-seen order and reject blanks like the Python configuration producer.

### PostgreSQL prefer negotiation

The shared native client retains available-root verification during a TLS
attempt. In the approved libpq18 domain, `prefer` may then try one fresh
plaintext connection after failed TLS negotiation or a server startup/auth
ErrorResponse other than `cannot_connect_now`. This matches libpq's transport
state, rather than removing CA checks from the TLS attempt. The existing
connect deadline covers both attempts; strict modes never retry plaintext.
The named policies `pg-dsn-explicit-tls` and `pg-tls-webpki-hostnames` remain
explicit, including the SAN-only divergence for verify-full. See PARITY.md
for pinned upstream source and historical versus candidate proof scope.
