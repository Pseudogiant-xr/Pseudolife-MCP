# Rust stdio shim candidate

`pseudolife-stdio` is the phase 1 candidate for the Python stdio shim.
The runtime is under implementation; complete parity is not yet accepted.
Behaviour and validation state
are recorded in [PORT-STATE.md](PORT-STATE.md), with the oracle contract in
[PORTING.md](PORTING.md) and the parity register in [PARITY.md](PARITY.md).

Build from this directory so `rust-toolchain.toml` selects Rust 1.94.0, after
checking the host's full-suite lease:

```text
cargo build --locked --jobs 4
cargo fmt --check
cargo clippy --locked --all-targets --jobs 4 -- -D warnings
cargo nextest run --locked --jobs 4
```

Phase 1 requires builds for `x86_64-pc-windows-msvc` and
`x86_64-unknown-linux-gnu`. Each platform needs its target toolchain and linker;
a Windows build alone provides no Linux validation evidence.

RMCP is pinned to 3.5.0, the prototype's SDK version, so an SDK upgrade does
not introduce another source of wire differences during the port. The candidate
must adapt SDK differences to the pinned Python oracle. Reqwest uses rustls
with default features disabled; each client must refuse redirects. JSON values
use `serde_json`'s arbitrary precision and ordered dictionary features to preserve
arbitrary integers and opaque JSON member order.
Cargo.lock records the resolved dependency versions.

The stdio differential lane compares retained stdout bytes under the named
`stdio-raw-compared` policy, stderr under its named-message allowlist, and exact
process exit codes. Its receipts establish only the recorded cases, platforms
and oracle pin; they do not establish installation, complete shim parity,
unmeasured performance, or daemon HTTP parity. The binary remains a harness
and CI candidate until later installation work is authorized.

The crate denies unsafe Rust by default. Platform modules have narrow,
documented exceptions: `credentials::windows_security` inspects the owner and
protected DACL through the live token-file handle, validates ACE/SID bounds and
keeps OS allocations under RAII until inspection finishes; `lifecycle::posix_session`
uses an allocation-free, lock-free `setsid` call in the child before exec to
preserve Python's `start_new_session` behavior. `board::doorbell_windows` owns
native job, process and thread handles for suspended adoption and cleanup;
`board::doorbell_posix` registers an execve-only callback after session setup,
with strings and pointer tables prepared before fork. Their exact invariants
and validation limits are recorded in [PORTING.md](PORTING.md). These exceptions
require security review before publication.

The executable accepts no mode, `shim`, or `channel`. It reads the Python shim's
configuration environment and forwards daemon tool operations over fresh MCP
HTTP sessions. Local daemon startup requires an explicit
`PSEUDOLIFE_MCP_PYTHON` interpreter
or `PSEUDOLIFE_MCP_SERVE_COMMAND` JSON argv; absent both, the candidate uses
the no-spawn wait and its named stderr explanation. The SDK preflight is
retired by the 2026-10-05 decision; remote daemon URLs never launch Python.
The candidate is launched directly from the build output during evaluation.

Targeted Rust cells cover EOF ownership, session deletion, sanitized transport
failures, cancellation, cursorless handshake caching, daemon recovery, exact
integer results and subscription acknowledgement order. They supplement the
public stdio differential lane; they do not establish complete parity. Board
and channel acceptance remains subject to their separate implementation and
validation streams.
