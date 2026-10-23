# Rust activation and rollback rehearsal, 2026-10-23 (Windows)

This tests whether a client would actually reach the Rust shim and daemon
after an install, and whether rolling back leaves both the executable choice
and the bank usable, on disposable state only. The launcher selected every
runtime correctly through a real client registration, and a bank written by
Rust was fully readable by Python after a rollback. Two things block a real
activation: the Rust daemon reports version `0.0.0`, which every shim refuses,
and the Python daemon stamps a bank from a newer build back down to its own
version without a warning.

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
- Result: `fail`, 23 of 24 steps. The one failure is the version refusal
  below, recorded as a non-fatal step so the rollback steps still ran.

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
   `runtime` line) and the console's SHA-256. Nothing ships a Rust shim to
   users today, so this proves the mechanism, not a shipped path.
2. **Durable state across Python -> Rust -> restart -> Python on one bank.**
   - Rust read the fact and found the entry Python wrote.
   - Rust wrote an entry, superseded that fact and set a new one, and read
     all three again after a restart.
   - After the rollback, Python read the superseded fact, the new fact and
     the Rust-written entry, and still found its own entry.
   - `meta.schema_version` stayed 56 through all four starts, and
     `writer_lease_epoch` counted each open (1 to 4).
3. **The Rust daemon refuses a newer bank.** With the stamp set to 57 (a JSON
   number, as `ensure_schema` writes it), Rust stayed up, not ready, logging
   "Refusing to start: bank schema version 57 is newer than this build's
   schema version 56."

## What did not

1. **No registered client reaches the Rust daemon.** The call through the
   registered client path (launcher -> Rust shim -> Rust daemon) was refused
   by the shim's version-skew check. The daemon's `/health` reports
   `version: 0.0.0`, which is `rust/daemon/Cargo.toml`'s crate version
   (`health.rs` uses `CARGO_PKG_VERSION`), while the shims are 0.19.0. The
   refusal tells the user to run `update --clients-only --tag 0.0.0`. Until
   the daemon's version follows the release, no installed client can use a
   Rust daemon.
2. **The Python daemon stamps a newer bank down.** On the same 57-stamped
   bank, the Python daemon started normally and rewrote `schema_version` to 56
   (`ensure_schema` upserts its own version and refuses no newer bank). A
   rollback to any older Python release, from Rust or from a newer Python,
   crosses this silently.
3. **The only opt-in Rust stack keeps no state.** `ops/docker-compose.rust.yml`
   puts the bank and `/data` on tmpfs, so it survives no restart and cannot
   carry an activation or a rollback. That is why this rehearsal runs the
   binaries directly instead.

## Not covered

- Linux and macOS: the driver has POSIX paths (the `/bin/sh` launcher,
  SIGTERM) but ran only on Windows here.
- The daemon's own install path (a container image or a release update):
  none ships the Rust daemon, so the daemon was started from the CI binary.
- Claude Code, Codex and Gemini registrations; hooks (the plugin's hooks call
  the daemon over HTTP and execute no shim binary except the doorbell).
