# Native client runtimes

The native runtime installer installs a new Rust CLI beside existing runtimes and
selects it for the next session. This is opt-in work; it does not change the
daemon engine default or remove the Python oracle or existing Python installs.

## Layout and completion

The stable command remains:

- POSIX: `$XDG_DATA_HOME/pseudolife-mcp/bin/pseudolife-mcp`, defaulting to
  `~/.local/share/pseudolife-mcp/bin/pseudolife-mcp`.
- Windows: `%LOCALAPPDATA%\pseudolife-mcp\bin\pseudolife-mcp.exe`.

Its sibling `runtimes` directory holds six-digit sequence directories,
preserving the existing `000001` ordering. Native binaries live directly in
the numbered directory, with the root binary name `pseudolife-mcp` or
`pseudolife-mcp.exe`; they do not use a virtualenv's `bin` or `Scripts` directory.
Sequence allocation counts incomplete and legacy directories too, so no prior
install is reused or overwritten.

Each native directory contains `runtime.json`, published last. Usage locks live
beside the directories as `.in-use-000001.lock`, allowing Windows to quarantine
a directory while retaining its exclusive lock. These small lock files are
retained after pruning. The marker has these fields:

| Field | Contract |
| --- | --- |
| `format`, `kind` | `1`, `"native"` |
| `version` | Release version without `v` |
| `installed_at` | UTC timestamp |
| `source` | Pinned release requirement or checkout path |
| `source_commit` | Checkout commit, otherwise null |
| `target` | One of the five R3 Rust target triples |
| `binary` | Exact platform binary basename |
| `binary_sha256` | Lowercase SHA-256 of the installed executable |
| `archive_sha256` | Verified release archive SHA-256, null for checkout builds |

Markers are owner-only files, read using the existing credential/state security
boundary. A native selector rejects redirected paths, malformed markers,
unexpected fields, wrong targets, missing executables, checksum mismatches and
non-executable POSIX files. It chooses the highest verified native sequence.
Legacy Python markers and incomplete or forged records never enter that set.
Verification checks consistency and ownership; it is not a release signature
or protection against a malicious process already acting as the installing user.

Both `PSEUDOLIFE_SHIM_RUNTIMES` and `PSEUDOLIFE_SHIM_LAUNCHER` must be supplied
together when overriding the layout. They must be absolute paths, and the
launcher must use the platform's executable suffix. Custom layouts do not
change PATH or create a user-bin link unless `PSEUDOLIFE_SHIM_USER_BIN` is set
on POSIX. All fixture homes use these overrides.

## Selection, activation and lifetime

The launcher is a copy of the native executable. `runtime::maybe_launch` runs
before normal CLI dispatch. At the stable launcher path it verifies/selects a
runtime and passes through every argument, the environment, working directory
and standard streams. A runtime path pins itself and proceeds with ordinary
dispatch; selection uses executable paths, not an environment flag that could
cause recursive launches or bypass verification.

POSIX uses `exec`, retaining signal delivery, exit status and terminal ownership.
Windows starts the child suspended through the existing `process-wrap` job
wrapper, assigns its job before execution, and uses kill-on-close ownership.
The launcher waits for the child and preserves its Windows process exit code;
console cancellation reaches the child rather than abandoning it. Per-runtime
shared usage locks remain held for the native child's lifetime.

Release installation downloads the platform release archive and `SHA256SUMS` over
HTTPS. The checksum must name the exact versioned asset once, using lowercase
hex and two spaces. Downloads and decompressed data are bounded. Archives may
contain exactly one regular root binary; links, traversal, additional files,
special entries and oversized payloads are refused. A checkout install builds
the Rust CLI using Cargo in its own temporary target directory, then installs
the artifact without executing Python, pip or pipx.

Installation holds `.native-update.lock`, reserves a fresh sequence, writes and
syncs the executable, activates the stable launcher, then atomically publishes
the marker. Windows renames the old stable executable aside rather than
overwriting a running image. If activation fails, its rollback guard restores
the previous launcher and the new sequence is removed. Retained
`*.old-native-*` launcher copies are recovery artifacts; the native updater
does not automatically delete them, because they can include a running legacy
Python launcher.

## Pruning and client state

Pruning considers only verified native directories. It retains the newest,
the updater's own runtime, process-held runtimes, registered paths and saved
tunnel snapshot dependencies. It also checks the native usage lock before
quarantining an old directory. Unknown process, registration or tunnel evidence
removes nothing. On Windows, an inaccessible process image is conservatively
unknown even if it probably belongs to another account, so pruning can retain
all old runtimes on a restricted host. Legacy Python runtimes, incomplete
directories and malformed markers are always retained for explicit recovery.

Registration migration backs up each changed file privately, preserves unrelated
entries and credentials, follows a dotfile symlink to its target, retains POSIX
permission bits and validates Windows ownership. A concurrent change is
preserved rather than overwritten by rollback. The only migrated arguments are
the leading `-m pseudolife_memory[.cli]` pair; mode arguments remain. Native
updates inspect legacy venv distribution metadata without running an
interpreter. Editable, spawning and unverified installs remain in place.

## Integration API

Export `pub mod runtime;` from the shim library. Call
`pseudolife_stdio::runtime::maybe_launch()` at the beginning of `main`, before
episode hooks or any other early dispatch, returning its `Some(u8)` as an exit
code. The Windows launcher preserves wider native exit codes internally.

```rust,ignore
runtime::maybe_launch() -> Option<u8>
runtime::run(args: Vec<OsString>) -> u8
runtime::default_layout() -> Result<Layout, String>
runtime::current_runtime(layout: &Layout) -> Option<Runtime>
runtime::running_runtime(layout: &Layout) -> Option<Runtime>
runtime::update_shim(source: &str, repo: Option<&Path>, reinstall: bool) -> Value

```

`Layout` exposes `root`, `launcher`, `user_bin: Option<PathBuf>` and
`user_path: bool`. `Runtime` exposes `path`, `sequence`, `version`, `source`,
`source_commit`, `marker`, `executable()` and `name()`. These are the lifecycle
and installer seams; they require no dispatch changes inside their consumers.

Installers can hold `runtime::UpdateLock::acquire(&layout)` while calling
`install_archive(&layout, version, source, archive_bytes, sha256sums_text)` or
`install_binary(&layout, version, source, source_commit, binary_path)` and then
migrating its registrations. Low-level install and prune functions require the
caller to retain that guard. `install_binary` is for trusted checkout artifacts;
release consumers must use `install_archive` for archive checksum verification.

The installer-facing command is:

```text
pseudolife-mcp runtime install --archive <archive> --sha256sums <SUMS-file> --version <version> [--json]
```

`runtime::run` receives arguments after `runtime`; the CLI dispatches this mode directly. It takes the same update lock and calls `install_archive`. The
archive and SUMS files are retained for installer retry/recovery. There is no
`--binary` release bypass. A successful JSON report has `state: "installed"`,
`runtime`, `version` and `launcher`; installation failures return a
`state: "failed"` report and exit 1. Invalid/missing/repeated options exit 2
before filesystem mutation. Setup owns the subsequent registration step.

## Recovery and validation

A failed download or checksum leaves the old launcher and runtime active. A
failed registration write retains its private backup and any newer concurrent
content. If the stable executable is locked during activation, release the lock
or retry after the scanner exits; running sessions need not be closed. If an
OS error also blocks rollback, preserve the `*.old-native-*` file and rerun
native setup from a separate CLI artifact; it reconstructs the stable path
from the selected complete runtime. Never repair by deleting runtime markers,
credentials, registrations or tunnel evidence.

The source includes isolated unit tests under `runtime::tests`, plus the `native_runtime_installed` integration
test. The integration test runs a production launcher in an empty PATH and
exercises two installed native sessions while updating side by side, Unicode
stdio and exit propagation. Build/execution evidence must be recorded separately;
formatting success does not prove these tests have run.
