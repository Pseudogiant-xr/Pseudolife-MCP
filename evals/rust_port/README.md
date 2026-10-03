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
wrong-port detector. Controls use stateless search/list/error requests on the
already seeded oracle bank, so they do not repeat storage mutations.

Historical schema-52 selfcheck R6 and full-bank R5 remain unchanged. Selfcheck
R4/R5 were archived outside the working tree and remain recoverable from PR
#540's original Git history. The source-reconstruction manifest retains the
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
the exact disjoint tokens changed by the declared identity/clock normalization.
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
