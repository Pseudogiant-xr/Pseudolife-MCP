# Native client runtimes

The native runtime installer installs a new Rust CLI beside existing runtimes and
selects it for the next session. This is opt-in work; it does not change the
daemon engine default or remove the Python oracle or existing Python installs.

## Checkout update wrappers

`ops/update.sh --native`, `ops/update.ps1 -Native`, or
`PSEUDOLIFE_NATIVE_UPDATE=1` explicitly selects the native updater. This path
does not probe Python or fall back after a native failure. The wrappers preserve
the selected updater's exit status and map the same checkout update flags.
`ops/update.py` also honors the environment opt-in for callers already using
Python.

Without explicit selection, the wrappers first probe for Python 3.10 or newer
as before. A usable Python installation remains selected even when the native
launcher is installed. If Python is unavailable or too old, the wrappers use a
native CLI only after its read-only `runtime --help` command succeeds and emits
the exact native runtime installer usage line. They then print one line to
stderr explaining the automatic selection. A legacy Python launcher, unrelated
executable, missing binary or failed probe does not qualify. This capability
check distinguishes compatible installed launchers; it is not an executable
signature or a new trust boundary for user-supplied paths.

Native path resolution uses `PSEUDOLIFE_NATIVE_CLI`, then
`PSEUDOLIFE_SHIM_LAUNCHER`, then the platform's stable launcher path below.
An invalid configured path refuses rather than trying a lower-priority path.
When neither runtime is usable, the refusal names both remedies: install the
native runtime, or install Python 3.10 or newer and the Python package. Wrapper
argument and runtime refusals leave `ops/.env` unchanged; Bash normalizes its
line endings only after selecting a usable updater.

Choosing the Python updater does not replace an active native client launcher.
Set `PSEUDOLIFE_SHIM_ENGINE=python` explicitly to activate Python clients; the
separate `--engine` option selects the Docker daemon. The Python helper prints
one stderr notice when it keeps or explicitly replaces a native launcher.

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
non-executable POSIX files. It verifies candidates newest first and chooses the
first valid native sequence, without hashing older retained binaries on launch.
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
The usage-lock descriptor survives this one exec; the runtime acquires its own
pin and marks the verified inherited descriptor close-on-exec before dispatch,
so detached children cannot retain an old session's pin.
Windows starts the child suspended, assigns its job before execution, and uses
kill-on-close ownership.
Explicitly detached daemon and updater launches can break away from that job;
ordinary child processes remain owned by it. The launcher waits for the child and preserves its Windows process exit code;
console cancellation reaches the child rather than abandoning it. Per-runtime
shared usage locks remain held for the native child's lifetime.

Release installation downloads the platform release archive and `SHA256SUMS` over
HTTPS from the GitHub release `v<version>`, or from the directory URL in
`PSEUDOLIFE_NATIVE_ARCHIVE_URL` (`http://` or `https://` with a host and no credentials,
query or fragment; any other value refuses before downloading, and every later download
or checksum error names the variable). Its `SHA256SUMS` comes from the same
directory, so the check proves integrity, not origin: use `http://` only on
loopback or a trusted network. The checksum must name the exact versioned asset once, using lowercase
hex and two spaces. Downloads and decompressed data are bounded. Archives may
contain exactly one regular root binary; links, traversal, additional files,
special entries and oversized payloads are refused. A checkout install builds
the Rust CLI using Cargo in its own temporary target directory, then installs
the artifact without executing Python, pip or pipx.

Installation holds `.native-update.lock`, reserves a fresh sequence, writes and
syncs the executable, activates the stable launcher, then atomically publishes
the marker. Windows renames the old stable executable aside rather than
overwriting a running image. If activation fails, its rollback guard restores
the previous launcher by atomic rename and the new sequence is removed. A
successful activation deletes its displaced launcher copy when the OS permits
it; a locked legacy image or failed rollback retains `*.old-native-*` for
recovery. Activation also invalidates the sibling Python launcher `.json` cache
record, restoring it only if the original launcher is restored on rollback.
Python's compatibility updater retains native and unreadable runtime directories
and allocates subsequent venv sequences beside them; it never selects native
markers as Python environments.

Python `ensure_launcher`, runtime installation and `client_updates.update_shim`
preserve an active native launcher by default, including unattended calls. A
compatibility update can install a newer Python venv without activating it.
To select the Python client runtime explicitly, set
`PSEUDOLIFE_SHIM_ENGINE=python` for that install or update invocation, for example:

```sh
PSEUDOLIFE_SHIM_ENGINE=python python ops/shim_runtime.py install --source <checkout>
```

This variable selects the client launcher; the update command's existing
`--engine` option selects the Docker daemon. `ensure_launcher` returns `kept`
when preserving the launcher, alongside its existing `written`, `replaced` and
`current` results. It emits one stderr notice when keeping or explicitly
replacing a native or unverified launcher. Its `current_runtime` helper and
`current` subcommand report the newest complete Python candidate, which can
remain inactive while the stable launcher is native.

Detection reads executable bytes without starting either runtime. POSIX ELF
and Mach-O signatures identify native launchers. On Windows, MZ alone is
ambiguous: Python recognizes its stored distlib `__main__.py` selector rather
than trusting a stale adjacent cache record. Unknown or malformed launchers
are retained unless Python is explicitly selected; unreadable launchers are
retained even with that selection. Native runtime directories and
`*.old-native-*` recovery copies are preserved during Python pruning.

An empty numbered directory is also retained: the native installer reserves
its sequence before writing an executable or marker, so age alone cannot
identify it as an abandoned Python install. Only unfinished directories with
Python venv structure are eligible for Python's age-based cleanup.

## Pruning and client state

Pruning considers only verified native directories. It retains the newest,
the updater's own runtime, process-held runtimes, registered paths and saved
tunnel snapshot dependencies. It also checks the native usage lock before
quarantining an old directory. Unknown registration or tunnel evidence and
ambiguous readable process evidence remove nothing. On Windows, inaccessible
system processes do not veto pruning when the exclusive runtime usage lock can
be acquired: a live native launcher holds the shared side of that lock. A
readable process referencing a runtime still retains it. Legacy Python runtimes, incomplete
directories and malformed markers are always retained for explicit recovery.

Registration migration backs up each changed file privately, preserves unrelated
entries and credentials, follows a dotfile symlink to its target, retains POSIX
permission bits and validates Windows ownership, accepting normal inherited
SYSTEM/Administrators access while rejecting other users' write access. A concurrent change detected
before publication is left in place. Publication
preserves the actual displaced file in an owner-only recovery directory, using
atomic file exchange on POSIX and `ReplaceFileW` with a backup on Windows.
Windows stages replacements through a private sibling link so inherited
permissions are resolved beside the target, then removes the redundant link.
If the displaced file differs from the observed input, the updater reports the
conflict and recovery location; it never rolls back over another writer. Cleanup
retains the three newest verified successful backups for each exact target.
Conflicted, unreadable, changed and unrecognized recovery evidence is retained;
failed cleanup may leave a `.client-native-pruning-*` quarantine directory. An
incomplete registration migration retains all runtimes. The only migrated
arguments are
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

`runtime::run` receives arguments after `runtime`; the CLI dispatches this mode
directly. It takes the same update lock and calls `install_archive`. The
archive and SUMS files are retained for installer retry/recovery. An installer
rerun reuses the newest verified runtime when its version, release source and
archive checksum match exactly; it repairs the launcher if needed.
The low-level installer still allocates a fresh sequence for explicit updater
reinstallation. There is no
`--binary` release bypass. A successful JSON report has `state: "installed"`,
`runtime`, `version` and `launcher`; installation failures return a
`state: "failed"` report and exit 1. Invalid/missing/repeated options exit 2
before filesystem mutation. Setup owns the subsequent registration step:

```text
pseudolife-mcp installer migrate-registrations [--client <name>]... [--json]
```

It refuses (exit 1, nothing changed) until a verified native runtime and the
stable launcher exist, holds the update lock, and moves `managed` and `legacy`
registrations (pipx, `pip --user`, a Python runtime venv) with `migrate`
above. `editable`, `spawning` and unrecognised registrations are reported and
left alone, and numbered directories holding a Python venv are named as the
old client left on disk. A rerun that changes nothing prints one line.

## Recovery and validation

A failed download or checksum leaves the old launcher and runtime active. A
failed registration write retains its private recovery directory and any newer
concurrent content, including the actual displaced file when publication raced
a writer. Review the reported recovery location before retrying. If the stable
executable is locked during activation, release the lock
or retry after the scanner exits; running sessions need not be closed. If an
OS error also blocks rollback, preserve the `*.old-native-*` file and rerun
native setup from a separate CLI artifact; it reconstructs the stable path
from the selected complete runtime. Never repair by deleting runtime markers,
credentials, registrations or tunnel evidence.

The source includes isolated unit tests under `runtime::tests`, plus the
`native_runtime_installed` integration test. The integration test runs a production launcher in an empty PATH and
exercises two installed native sessions while updating side by side, Unicode
stdio and exit propagation. Build/execution evidence must be recorded separately;
formatting success does not prove these tests have run.

The installers' Python tests run the real client where one is given:
`PSEUDOLIFE_NATIVE_TEST_BINARY` names a native `pseudolife-stdio` built from
the same commit, and `PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED=1` makes a test
that needs it fail instead of skip. CI's Python lanes that run installer
tests (`test`, `test-lite-windows`, `test-lite-macos`, the Windows nightly)
build one before their tests and set both (`tests/test_ci_native_client.py`
pins it); on macOS this is the native client's only CI execution. Locally,
set them to a `cargo build -p pseudolife-stdio` output to run the same
path.
