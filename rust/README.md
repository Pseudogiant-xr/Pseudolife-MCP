# Rust workspace

This workspace holds the Rust implementation of Pseudolife-MCP. Its daemon is
what a Docker install runs; its client is built and tested but not yet what
the installers put on a user's machine. [CUTOVER.md](CUTOVER.md) records
where the move from Python stands and what is left.

| Path | What it is |
|---|---|
| `daemon/` | `pseudolife-daemon`, the HTTP service that owns a bank: the MCP tools, the Console and hook routes, the board, dreams and background duties. Published as `pseudolife-daemon-rust` and the default engine for Docker installs since 0.23.0. |
| `shim/` | The package `pseudolife-stdio`: the stdio MCP bridge to the daemon and the native `pseudolife-mcp` command. The Rust daemon image ships it as `pseudolife-mcp`; on a user's machine it is opt-in (`pseudolife-mcp runtime install`). |
| `shared/` | Modules both crates include: PostgreSQL runtime and TLS helpers, Windows file-security checks, float formatting. |
| `cli_harness/`, `daemon/harness/`, `extractor_harness/` | Python harnesses that run the Rust binaries and compare them with committed recordings (goldens). They were built to compare with the Python implementation, which is frozen since 2026-11-14; the goldens now serve as regression snapshots. |
| `parity/`, `daemon/divergences/`, [PORT-STATE.md](PORT-STATE.md), [PORTING.md](PORTING.md), [PARITY.md](PARITY.md) | The register of what was ported and where Rust knowingly differs from Python. A record of the port, no longer an acceptance gate. |
| `ci/` | Scripts and tests behind `.github/workflows/rust.yml`: scope, budget, stress and timing. |

## Building

Build from this directory so `rust-toolchain.toml` selects Rust 1.94.0, after
checking the host's full-suite lease ([DEV.md](DEV.md) has the local loop):

```text
cargo build --locked --jobs 4
cargo fmt --check
cargo clippy --locked --all-targets --jobs 4 -- -D warnings
cargo nextest run --locked --jobs 4
```

Releases build `x86_64-pc-windows-msvc`, `x86_64-unknown-linux-gnu`,
`aarch64-unknown-linux-gnu` and both macOS targets. Each platform needs its
target toolchain and linker; a Windows build alone is no Linux evidence.

## Dependencies worth knowing

RMCP is pinned to 3.5.0 so that an SDK upgrade is a deliberate change to the
wire behaviour. Reqwest uses rustls with default features disabled, and each
client refuses redirects. JSON values use `serde_json`'s arbitrary precision
and ordered dictionary features, to preserve arbitrary integers and opaque
JSON member order. `Cargo.lock` records the resolved versions.

## Unsafe code

The crates deny unsafe Rust by default. Platform modules have narrow,
documented exceptions: `credentials::windows_security` inspects the owner and
protected DACL through the live token-file handle, validates ACE/SID bounds
and keeps OS allocations under RAII until inspection finishes;
`lifecycle::posix_session` uses an allocation-free, lock-free `setsid` call in
the child before exec; `board::doorbell_windows` owns native job, process and
thread handles for suspended adoption and cleanup; `board::doorbell_posix`
registers an execve-only callback after session setup, with strings and
pointer tables prepared before fork. Their exact invariants are recorded in
[PORTING.md](PORTING.md). Review any change to them as security-sensitive.

## The shim at runtime

With no mode, or `shim` or `channel`, the binary is the stdio bridge: it reads
the client's configuration environment and forwards daemon tool operations
over fresh MCP HTTP sessions. Remote daemon URLs never start a process. A
local daemon is started only from an explicit `PSEUDOLIFE_MCP_PYTHON`
interpreter or a `PSEUDOLIFE_MCP_SERVE_COMMAND` JSON argv; with neither, the
shim waits for a daemon and says so on stderr.

Every other mode is a `pseudolife-mcp` command. A command or shape that is not
ported prints `mode '<mode>' is deferred in this candidate` and exits 1; it
never runs Python. [CUTOVER.md](CUTOVER.md) lists the ones left.
