# Rust activation and rollback rehearsal, 2026-10-23 (Windows)

This tests whether a client would actually reach the Rust shim and daemon
after an install, and whether rolling back leaves both the executable choice
and the bank usable, on disposable state only. 23 of 24 steps held:

- The launcher selected every runtime correctly through a real client
  registration.
- A call through that registration reached the Rust daemon.
- A bank written by Rust was fully readable by Python after a rollback.

Two things still need fixing before a real activation. The Python daemon
stamps a bank from a newer build back down to its own version without a
warning. And the Rust daemon reports version `0.0.0`, so the shim tells the
user to install a release that does not exist.

- Driver: `rust/daemon/harness/rehearse_activation.py` (this PR), run on
  demand from a working tree on master `bd8217a1`:
  `python rust/daemon/harness/rehearse_activation.py --rust-shim
  pseudolife-stdio.exe --rust-daemon pseudolife-daemon-static.exe --out
  windows.json`.
- Binaries: the `rust-shim-Windows` artifact of master `bd8217a1`'s Rust
  workflow run 39749787387. The daemon is `pseudolife-daemon-static`, the
  default build; the artifact's `pseudolife-daemon` is rebuilt with test-only
  features. Their SHA-256 values are in `windows.json`.
- Host: the maintainer's Windows 11 host, natively, in a disposable venv over
  the system packages with CI's pins added (`mcp==2.2.0`, `onnx==1.22.0`,
  `onnxruntime==1.27.0`, `optimum==2.1.0`, `optimum-onnx==0.1.0`). It used one
  disposable database on the bench PostgreSQL (`pgdisposable`, dropped at the
  end) and one disposable home.
- Embeddings: both daemons use the same 1,024-dimension ONNX fixture (run.py's
  CI fixture), so this proves durable-state compatibility, not model parity.
- Result: `fail`, 23 of 24 steps. The one failing step is an expectation
  today's code does not meet: the Python daemon leaves a newer build's
  schema stamp alone (below). Every daemon stop exited 0. The evidence
  records the driver's SHA-256 and the uncommitted files of the tree it ran
  from (this PR's files, before they were committed).

## What held

1. **Executable selection, through the real mechanism.** These steps used
   the real pieces:
   - a real Python shim runtime: this checkout, pip-installed without
     dependencies into a venv;
   - the real launcher (`runtimes.ensure_launcher`, the Windows selector);
   - a real Claude Desktop registration (`ops/register_claude_desktop.py
     --config`, whose own `--help` probe ran through the launcher).

   The registered command then ran, in turn:
   - runtime 000001 (Python);
   - after the Rust shim was installed as 000002 beside an incomplete 000003,
     000002;
   - after a rollback install of the older code as 000004, 000004;
   - after 000004 was removed, 000002 again.

   Which binary answered came from the shim's own `version` output (its
   `runtime` line), and the SHA-256 of that runtime's console is recorded:
   the Rust shim's at 000002, a Python console at 000001 and 000004. Nothing ships a Rust shim to
   users today, so this proves the mechanism, not a shipped path.
2. **The registered client path reaches the Rust daemon.** An MCP session
   over stdio through the registered command (launcher -> Rust shim -> Rust
   daemon) read a fact the Rust daemon had written. It initialized, waited
   for the answer, made the call and waited for its reply before closing
   stdin, as a client does; the Rust shim treats EOF as the session ending.
3. **Durable state across Python -> Rust -> restart -> Python on one bank.**
   - Rust read the fact and found the entry Python wrote.
   - Rust wrote an entry, superseded that fact and set a new one, and read
     all three again after a restart.
   - After the rollback, Python read the superseded fact, the new fact and
     the Rust-written entry, and still found its own entry.
   - `meta.schema_version` stayed 56 through all four starts, and
     `writer_lease_epoch` counted each open (1 to 4).
4. **The Rust daemon refuses a newer bank and leaves it as it was.** With the
   stamp set to 57 (a JSON number, as `ensure_schema` writes it), Rust stayed
   up, not ready, logging "Refusing to start: bank schema version 57 is newer
   than this build's schema version 56", and the stamp still read 57 after it
   stopped.

## What still needs fixing

1. **The Python daemon stamps a newer bank down.** On the same 57-stamped
   bank, the Python daemon started normally and rewrote `schema_version` to 56
   (`ensure_schema` upserts its own version and refuses no newer bank). A
   rollback to any older Python release, from Rust or from a newer Python,
   crosses this silently. This is the rehearsal's failing step: the
   expectation stays in the driver, so the rehearsal passes once the Python
   daemon refuses a newer bank or leaves its stamp alone.
2. **The version notice points at a release that does not exist.** The Rust
   daemon's `/health` reports `version: 0.0.0`, which is
   `rust/daemon/Cargo.toml`'s crate version (`health.rs` uses
   `CARGO_PKG_VERSION`), while the shims are 0.19.0. The call still succeeds,
   but the shim prints a mismatch notice telling the user to run `update
   --clients-only --tag 0.0.0`. The daemon's version should follow the
   release.
3. **The only opt-in Rust stack keeps no state.** `ops/docker-compose.rust.yml`
   puts the bank and `/data` on tmpfs, so it survives no restart and cannot
   carry an activation or a rollback. That is why this rehearsal runs the
   binaries directly instead.

An earlier run of this driver misread the second item as a refusal. Its
client closed stdin straight after sending the call; the Codex review caught
it, and this evidence is from the corrected driver.

## Not covered

- Linux and macOS: the driver has POSIX paths (the `/bin/sh` launcher,
  SIGTERM, the ONNX Runtime `.so` or `.dylib`) but ran only on Windows here,
  so they are unproven.
- The daemon's own install path (a container image or a release update):
  none ships the Rust daemon, so the daemon was started from the CI binary.
- Claude Code, Codex and Gemini registrations; hooks (the plugin's hooks call
  the daemon over HTTP and execute no shim binary except the doorbell).
