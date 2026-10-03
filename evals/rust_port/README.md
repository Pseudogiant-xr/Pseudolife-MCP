# Rust port differential instrument

This records requests and observed responses across whole CLI processes and
HTTP/MCP connections, then replays the same requests against a candidate.
It also provides an external pytest adapter for five explicitly mapped existing
CLI assertions. Existing tests and `tests/conftest.py` stay unchanged.

## Proven scope

The corrected receipts below use historical Python source
`136a34ae95e981a691fcc31ba9fb4f35d83d4249`, schema 52. The integration branch
subsequently moved to `d398068743e7276e884bd2693501484b83f981e9`, which declares
schema 53 and changes principal storage, HTTP authentication and MCP tier
resolution. The provisional captures are preserved privately; the corrected
captures use the current instrument and do not validate that new production
source. Current-source daemon or fixture behavior has not been rebaselined.

`selfcheck`, `oracle` and the full-bank runner require historical source by
default. Select a separate clean checkout with `--oracle-root`; the current
instrument runs there with production imports and subprocess working directories
pointing at that checkout. The `evals` package resolves only from the current
instrument checkout, including the external pytest plugin and process helper;
an older `evals/rust_port` package in the oracle checkout cannot replace it.
The source check covers production, dependency
declarations and the existing test files used by the adapter, including local
tracked/untracked source drift. It allows an instrument-only Git commit while
refusing changed oracle code or a schema other than 52. No schema is migrated.
New metadata distinguishes `source_head`/`source_schema` from `instrument_head`,
names the historical oracle separately and hashes the current instrument,
the process ownership helper and the native binding module it uses. It
never emits the local checkout path. Dependencies are recorded, not silently
pinned or upgraded; reconstruct the versions in the historical receipt for an
equivalent dependency lane. `prepare` can inspect either tree without importing
the daemon and explicitly reports that no requests executed.

The [source reconstruction manifest](../results/rust-rewrite-source-reconstruction.json)
maps every historical source digest in baseline R5, full-bank R5 and selfchecks R4/R5
to a canonical Git blob and its exact line endings. Read a blob with
`git cat-file blob <git_blob_oid>`, verify `canonical_blob_sha256`, then replace
the final LF with CRLF only on the one-based lines in `crlf_line_ranges`
(inclusive ranges). Leave all other bytes unchanged and verify
`recorded_raw_sha256`. This also preserves the mixed line endings in
`test_processes.py`; a uniform conversion does not recover its captured digest.
The manifest identifies historical oracle files separately and leaves every
capture unchanged. Apply the same recipe to `receipts` and `referenced_artifacts`
before checking their historical `sha256`/`recorded_raw_sha256`: together they
cover all thirteen tracked captures, including both corpus and oracle files. Four
baseline receipts require the declared CRLF ranges; the nine port artifacts use
LF. Git objects and these recipes suffice on a clean Linux checkout. Untracked
executable hashes and semantic synthetic-corpus digests are separate observations.
Resolve sources by path plus raw digest, since R4 and R5 retain different
versions of the oracle and runtime fixture. Older versions declare their
`source_commit`; never replace an old binding merely because its path repeats.

Runtime provenance distinguishes `parent_runtime` from `actual_child_runtime`.
The fixture child reports its own metadata; the full-bank adapter retains the
daemon's runtime observation from identity-verified health in each arm's cleanup.
Interpreter basename/hash, installed distribution versions, package runtime
version and selected source-file match/hash contain no absolute paths. CLI and
pytest adapter runtime probes are explicitly separate processes with the same
interpreter, environment and selected source, rather than observations written
inside the tested CLI. Isolating APPDATA can hide user-site distribution metadata
and expose an older base installation even when production source is pinned.
Earlier receipts contain parent metadata and are preserved without relabeling.

`seed.json` plus `fixtures.py` generate a synthetic in-memory bank and ordered
request corpus. `oracle.py` runs the real Python `web.api` routes and registered
tools in an owned loopback process. The production daemon entry point constructs
the transport above a synthetic `MemoryService` constructor seam; eval code does
not import or inspect the MCP projection. Fixture health and service background
callbacks remain synthetic, and the listener uses the pre-bound owned socket.
The CLI arm runs `pseudolife_memory.cli` in
separate processes. No database, embedding model, live bank or client settings
are used. The service seam validates transport, tool schema and payload
projection, **not retrieval, storage, authentication or daemon lifecycle parity**.

`selfcheck.py` requires a compiled negative fixture, records the Python oracle,
passes Python replay and rejects `broken_fixture.rs` through CLI mismatches.
Its HTTP/MCP replay target remains the Python fixture process; the negative
control does not claim a Rust MCP or HTTP implementation. `--validate-plugin`
also runs the same five existing assertions through Python subprocesses and the
broken compiled executable, and verifies unmapped selected tests are refused.
Source hashes demonstrate that the selected test file and conftest were not
edited. No production surface is marked ported by these checks.

The phase-0 receipt, request corpus and oracle transcript are under
`evals/results/rust-port-phase0-selfcheck-r5/`; R4 remains preserved as historical
evidence with its original sources. None of the eleven cases reaches a real
bank or model: two use the real Python CLI, four use the
real HTTP API with a fixture service, and five use real MCP handlers with that
same service seam. The separate full-bank control below now exercises the real
daemon, embedding model and PostgreSQL storage.

## Real PostgreSQL-bank control

`full_bank.py` and `full_seed.json` generate three independent synthetic memories
and a 25-case corpus. The adapter reuses `rust_baseline.daemon`'s guarded database
creation and owned-process launcher, records requests on one fresh bank, then
replays on a second fresh bank. It exercises bearer rejection, authenticated MCP
initialize/list/call, store and empty-store behavior, unknown-tool error, MCP and
REST search, fact insertion/supersession/read/history, coordination registration,
ordered send/receive, acknowledgment and identity/error rejection. CLI coverage
and the compiled Rust negative control remain in the separate selfcheck above.

The [full-bank receipt](../results/rust-port-phase0-full-bank-r5/run.json) records
25 passing cases, zero differences and cleanup of both daemons, their children
and both databases. The [oracle transcript](../results/rust-port-phase0-full-bank-r5/python-oracle.json)
contains every request and normalized response. This is functional Python-to-Python
validation, not performance measurement or Rust-daemon parity. It uses the real
CPU torch embedder; ONNX parity and the complete surface corpus remain future work.

```sh
python -m pytest evals/rust_port/test_harness.py evals/rust_port/test_full_bank.py -q
python -m evals.rust_port.full_bank prepare --oracle-root /path/to/python-oracle --out-dir /tmp/contract-prepared
python -m evals.rust_port.full_bank run --oracle-root /path/to/python-oracle --out-dir /tmp/contract-run --board-checked-at VERIFIED_FREE_BOARD_UTC --score-abs-tol 0.000001
```

Run only after the lead grants the CPU slot and verifies resource clearance;
the launcher also checks the local lease. When the board is unavailable, the
mutually exclusive `--offline-resource-checked-at VERIFIED_OFFLINE_CLEARANCE_UTC`
option records independent local/WSL lock and peer workload clearance. It never
invents a board timestamp or overrides an available busy board or local lock.
No caller-supplied DSN is accepted. Private directory removal uses the launcher's
bounded filesystem retry after owned-process shutdown.
Every arm gets private home/configuration directories; only the explicit offline
model artifact cache is shared. Bearer and agent credentials are injected at
runtime, never stored in the corpus, and a credential escape rejects publication.

Normalization names only fields in the declared operation shapes. Agent/message
UUIDs use bijective symbols; later references must resolve to the same identity.
Credential fields become vault symbols. Epoch clock symbols retain their exact
integer or float type, including captures and later references; booleans are
rejected. Fact stamps must agree across write/read/history. Mail
creation/expiry must agree across receipts and reads, have the expected lifetime,
and acknowledgments must fall within it. Mail sequence numbers, cursor positions,
message order, text and continuity fields remain exact, with strictly advancing
HLCs checked before their wall-clock-dependent stamps become symbols. Fact values
and version ordering remain exact. Human age strings and compact calendar dates
are compared exactly; a run crossing their rendering bucket fails honestly.
Search returns all three distinct integer storage IDs, which remain exact and
ordered independently of the configurable score tolerance. Both MCP text JSON
and structured content are compared. Unknown-tool error text is compared as text.

## Run

Use a dependency environment containing the repository's Python requirements.
Before compiling, check `pseudolife-mcp lease check full-suite`; respect the host's
resource rules. The fixture has no crate dependencies and uses one codegen unit;
on a shared Windows host launch the compiler at BelowNormal priority, with a
bounded job count. Compile it once to a disposable directory:

```sh
rustc evals/rust_port/broken_fixture.rs --edition 2021 -C codegen-units=1 -o /tmp/rust-port-broken
python -m pytest evals/rust_port/test_harness.py -q
python -m evals.rust_port.selfcheck --oracle-root /path/to/python-oracle --broken-binary /tmp/rust-port-broken --out-dir /tmp/rust-port-proof --validate-plugin
```

Use a fresh output directory on every run. Artifact writes use exclusive create
and refuse to overwrite existing evidence. The selfcheck stops and waits for its
owned oracle process before writing the success receipt; private process logs
and client homes are temporary and removed. Evidence contains responses and
safe path/reason differences, without executable paths or target origins.

The CLI, pytest adapter and fixture oracle establish subtree ownership before
child code runs and reclaim it on success, timeout and startup failure, including
when the direct parent has already exited. Windows uses a suspended launch and
job assignment before resume, then waits for the job's active process count to
reach zero. Readiness can verify the live CPython PID behind a virtualenv
redirector through exact opened-handle PID equality and membership in that Job;
invalid, overflowing, aliased and unrelated PIDs are refused. It refuses unavailable ownership instead of using a descendant
snapshot or `taskkill` fallback. POSIX uses a new process group and waits for its
running members to exit; runtime membership requires that group and a live
non-zombie process. Deliberately detached sessions are unsupported. The
new cleanup and routing regressions exercise real disposable subprocesses; the
linked receipts bind the execution sources present at capture time by hash.
Subsequent instrument fixes require fresh captures; historical receipts remain
unchanged.

To record your own disposable target, generate a corpus and start its server,
then supply a JSON argv prefix and explicit loopback origin:

```sh
python -m evals.rust_port.fixtures > /tmp/corpus.json
python -m evals.rust_port.harness record --input /tmp/corpus.json --out /tmp/oracle.json --cli-json '["python", "-m", "pseudolife_memory.cli"]' --base-url http://127.0.0.1:19001
python -m evals.rust_port.harness replay --input /tmp/oracle.json --out /tmp/diff.json --cli-json '["./candidate"]' --base-url http://127.0.0.1:19002
```

Replay exits 1 on differences; boundary timeouts, malformed responses,
abnormal termination and launch failures are negative evidence. CLI observations
admit only integer exit codes in the portable ordinary range 0 through 255,
including nonzero CLI errors. POSIX signals and Windows exception/NTSTATUS
termination cannot become an oracle or pass candidate replay. A record containing boundary errors
cannot be used as an oracle. CLI stdout/stderr are byte-preserving base64
fields alongside the exact exit code. Requests contain argv or relative HTTP
paths/methods/bodies, never executable paths, target URLs or credentials.
Use only curated synthetic input: transcripts are public artifacts, so do not
include real names, addresses, secrets or private text in requests/responses.

## Comparison contract

JSON mapping key order is semantic; every array retains order and multiplicity.
Numbers are exact unless a named score path opts into tolerance. Each case's
`policy` is persisted with the request:

```json
{
  "abs_tol": 0.000001,
  "rel_tol": 0.0,
  "score_paths": ["/body/entries/*/score"],
  "ranking_paths": ["/body/entries"],
  "ranking_key": "id",
  "ignored_values": ["/body/generated_at"],
  "json_text_paths": []
}
```

Paths use JSON Pointer escaping and one-segment wildcards. `ignored_values`
ignores only a present scalar's value while checking its type and preserving
presence; it cannot remove a field or ignore an object/list. No timestamp, ID,
whitespace, string case or numeric normalization happens by heuristic.
NaN/infinity fail, including at ignored scalar paths. Ranking IDs are compared
in strict order independently of score tolerance, and ordinary array comparison
also remains strict. MCP JSON payloads inside text blocks are parsed only at
explicit `json_text_paths`; both text content and structured content are checked.

HTTP response status, body and semantic `content-type`, `cache-control` and
`location` headers are recorded. Incidental transport headers such as Date,
Server, Content-Length and SDK-generated session IDs are outside this initial
instrument's comparison scope. SSE data envelopes retain arrival order;
SSE framing IDs/retry directives are not checked. MCP session IDs are negotiated
anew for each pass, not replayed as credentials, and the negotiated protocol
version is sent on subsequent requests. Redirects and environment proxy
discovery are disabled. Authentication headers are refused in recordable input;
the full-bank adapter supplies authenticated synthetic credentials privately.
Only disposable loopback HTTP origins are accepted by this phase-0 instrument.

## External pytest selection

`oracle_tests.json` is an explicit node manifest. Without `--port-cli-json`, the
plugin leaves Python tests untouched. With that option, selected mapped tests
replace their imported `main` entry point with a whole CLI process invocation,
copy the candidate's bytes to the captured streams as UTF-8 text, and raise
`SystemExit` with its exit code. Assertions and expected errors remain the
original test code; no candidate comparison is delegated to the candidate.

```sh
python -m pytest -p evals.rust_port.pytest_plugin tests/test_cli_dispatch.py --port-cli-json '["./candidate"]' -k 'help or unknown_mode' -q
```

Selecting an unmapped node in candidate mode exits with pytest usage error 4;
no tests are silently skipped or deselected by the plugin. Version/runtime
monkeypatch cases are explicitly unmapped. HTTP, stdio shim, storage and other
test pools still need audited external adapters. The five mappings establish
only help/unknown-mode behavior, not complete CLI parity.

## Recorded protocol coverage

The selfcheck requests MCP `2026-07-28` and records the version Python actually
negotiates. In the initial environment, SDK `2.1.1` negotiated `2025-11-25`.
The transcript preserves that observed legacy response. July 2026 uses
`server/discover` and reserved per-request `_meta`, so an `initialize` request
does not test modern protocol support. Phase 1 needs a separate revision matrix
and modern subscription proof; these phase-0 captures establish the legacy lane.
Score tolerance here tests the comparator; it does not establish embedding
tolerance or same-graph ONNX equivalence.
