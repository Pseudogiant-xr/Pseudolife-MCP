# Optional Rust HTTP transport

The stdio shim can use a persistent Rust process for its MCP HTTP exchanges.
Python still owns the MCP SDK, credentials, session identity, coordination,
channel handling, timeouts and recovery policy. The daemon stays in Python.
The existing Python HTTP transport remains the default.

Build from a checkout with Rust 1.94.0 (the validated toolchain):

```sh
cargo build --release --locked -j1 --manifest-path rust/http-transport/Cargo.toml
```

Set `PSEUDOLIFE_MCP_RUST_HTTP` to the absolute path of the resulting
`pseudolife-http` executable (`pseudolife-http.exe` on Windows) in the shim's
environment, then start the existing shim command. Removing that variable
selects the Python transport. An explicit missing or unusable executable fails
closed. Installers, entry points, plugin configuration and daemon deployment do
not select or distribute the helper; this is an opt-in source build.

## Contract boundary

One helper serves one stdio session. Each operation still creates its existing
fresh MCP client/session, so idle daemon restarts do not retain stale HTTP
connections. Python sends the SDK's HTTP method, URL, complete body, duplicate
headers and resolved proxy over private pipes. Rust returns status, headers and
streamed bytes; it does not interpret MCP capabilities, tool names, JSON-RPC,
session metadata, wake decisions or credentials. New fields therefore cross
this boundary as bytes, subject to the unchanged Python SDK/policy behavior.

The private protocol is versioned JSON lines with base64 byte fields. Four
16 KiB response chunks per stream and credit messages bound queued pipe data.
All helper stdout is protocol; diagnostics are fixed text on stderr. Credentials
are absent from subprocess arguments and errors. The helper pins the configured
origin, rejects URL credentials, follows no redirects and disables HTTP retries.
Cancellation closes the response and cancels the matching request. Failure after
dispatch never falls back to Python or replays a request; the Python shim retains
its existing unknown-outcome classification. A later operation can start a new
helper after a crash.

System certificate trust uses the native TLS backend; platform CI must validate
its behavior, and this is not a claim of exhaustive trust-store equivalence.
Configured `SSL_CERT_FILE` or `SSL_CERT_DIR` selects the existing Python HTTP
transport before requests. That preserves the original complete trust input,
including explicitly trusted leaf certificates and mixed CA/leaf stores, which
a CA-only projection would omit. Python retains its `SSL_CERT_FILE` precedence
over `SSL_CERT_DIR`. Python also handles bodies above the helper's
32 MiB pipe envelope, helper capacity overflow, SOCKS proxies, and requests with
different write/read timeout values. Changes to certificate-file material or
trust environment are revalidated before the next send and select Python while
the helper retains its earlier trust snapshot. These decisions occur before dispatch and
preserve the existing transport's capabilities. The shim's normal write/read
timeouts are equal and its total operation deadline remains authoritative.

HTTP proxy selection and `NO_PROXY` matching use the same Python httpx2 resolver
as the default path; only the chosen proxy reaches Rust. Episode HTTP posts and
the coordination adapter's registration, attach, heartbeat, renewal and wake
delivery remain on their existing Python clients.

## Parity and migration boundaries

| Contract | Owner and executable evidence |
| --- | --- |
| Protocol revision, initialize instructions, tool schemas, pagination, typed results, resources/prompts method-not-found | Python SDK/shim; paired real stdio test in `tests/test_rust_transport.py` |
| Codex `_meta` identity and writer/session/agent/key/bank/principal headers | Python policy; paired real stdio identity assertions |
| Opaque payloads and headers, duplicate response headers, streaming, HTTP proxies | Rust HTTP slice; paired disposable HTTP fixtures |
| HTTPS untrusted rejection; configured CA, leaf and mixed trust acceptance; directory trust and oversized requests | Native rejection and paired pre-send Python selection fixtures |
| Credential rotation and precedence, sanitized HTTP errors, dropped connection after commit, idempotency, total deadlines, SSE limits, cancellation, next-operation recovery | Unchanged Python policy; existing `tests/test_shim_transport_recovery.py` also runs with the Rust helper selected |
| Persistent helper, crash/restart, response capacity cleanup | Rust/Python adapter; process and repeated-response fixtures |
| Channel notifications, board registration/renewals, subscription lifetime and wake policy | Existing Python owners and their tests; these clients are outside this slice |
| Daemon toolsets, principal admission, storage and all durable-state behavior | Existing Python daemon; no migration in this change |

Run the transport tests after building the debug helper:

```sh
cargo build --locked -j1 --manifest-path rust/http-transport/Cargo.toml
cargo test --locked -j1 --manifest-path rust/http-transport/Cargo.toml
python -m pytest tests/test_rust_transport.py tests/test_shim_transport_recovery.py -q
```

Set `PSEUDOLIFE_MCP_RUST_HTTP` to that debug binary to run the existing recovery
file through Rust. `PSEUDOLIFE_RUST_TEST_BINARY` can point the new tests at a
different build. New Rust integration cases skip if no binary exists; the
dedicated Linux/Windows/macOS CI lane builds it first and runs client-only real
stdio fixtures. Only Windows has been validated locally; other platforms need
the CI result. The usual full CPU suite remains a separate pre-commit gate.

## Performance comparison and next work

There is no speed or memory improvement claim for this slice. Python/SDK startup
remains, and an extra process, JSON/base64 copies and native TLS state may cost
more memory or latency. Historical prototype timings are not a comparator.

`evals/bench_rust_transport.py` compares alternating Python and Rust arms of the
same current stdio session: initialize, tools/list, ordinary calls, a 1.4 MB SSE
result, and concurrent calls. It records source/binary/dependency hashes, per-call
times and sampled aggregate RSS of the Python shim plus all helper processes.
The common driver and fixture server are excluded. Sampled RSS can miss short
peaks. Each run requires a fresh output file and verifies owned-process cleanup:

```sh
python evals/bench_rust_transport.py --rust-binary <absolute-debug-or-release-binary> --out <fresh-tagged-result.json>
```

Two tagged Windows CPU comparisons are committed in
`evals/results/rust-transport-cpu-20261002-a.json` and
`evals/results/rust-transport-cpu-20261002-b.json`. They compare complete parsed
MCP outputs and peak simultaneous RSS over the whole owned process tree. Their
fingerprints identify the measured adapter stages, which precede the final
capacity-reservation correction; they do not benchmark that later revision.
The results do not demonstrate a combined latency and total-memory benefit: the
Python host remains, and the helper adds memory. Four paired trials against a
local fixture cannot establish production throughput or cross-platform gains.
A real daemon workload remains a further gate before any cutover. The existing
overnight supervisors and ledger remain the long-run orchestration tools.

Disposable composition checks cover the exact published heads of peer PRs
#514, #515, #516 and #517, including parent/child hint handling, urgent wake
policy, Codex attribution and health/expose behavior. Features whose PRs are
not yet published still require their own composition checks.

Next migration work is to carry the boundary tests onto a standalone Rust MCP
shim. That shim must first port SDK/session/subscription semantics, coherent
credential snapshots, board and channel lifecycle and CLI contracts. A daemon
rewrite then needs separate tool/admission, storage, recovery and upgrade parity
gates. Default selection or binary distribution requires its own reviewed
change after those gates.
