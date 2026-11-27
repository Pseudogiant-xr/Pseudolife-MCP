# Native client runtimes

The native runtime installer installs a new Rust CLI beside existing runtimes and
selects it for the next session. The installers select it by default since
0.26.0; it does not change the daemon engine default or remove the Python
oracle or existing Python installs.

[Finish the move to Rust](python-retirement.md) maps the work after the 0.24
cut: the open installer stack, remaining client workflows, package-deletion
dependencies and installed acceptance checks.

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
quarantining an old directory. Unknown registration or tunnel evidence removes
nothing. A process whose image cannot be read does not veto pruning when the
exclusive runtime usage lock can be acquired, since a native runtime started
from its own directory holds the shared side of that lock: on Windows a
process that refuses `OpenProcess`, on Linux one whose `exe` link refuses this
account (a non-dumpable process), on macOS one that `ps` names without a path.
On Windows a readable image that is not an absolute path, and on macOS a
pathless name that is the runtime's own file name, remove nothing. A readable
process referencing a runtime still retains it. Legacy Python runtimes, incomplete
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

## Installers

`ops/install.sh` and `ops/install.ps1` install the native client by default
and with `PSEUDOLIFE_SHIM_ENGINE=native`. `PSEUDOLIFE_SHIM_ENGINE=python`
installs the Python client, which also brings a native machine back, and
records the choice as `python` in `~/.pseudolife-mcp/client-engine` once
the install has run to its end (on Windows under `USERPROFILE`, where
Python's `Path.home()` looks too). A later run without the variable honours
that file when its text, trimmed at both ends, is exactly `python`, and a
native install removes it. Any other value of the variable is refused
before anything changes.

A defaulted run tells the native installer so: bash runs it with
`PSEUDOLIFE_INSTALLER_DEFAULTED=1` (exactly that value; an explicit
`=native` clears it), PowerShell sets the global `PseudolifeClientDefaulted`
and reads the global `PseudolifeClientFallback` back (both cleared after the
run). When the native client cannot serve the machine, is not installed
there already, and nothing on the host has changed, the native installer
hands back. In bash it runs `ops/install.sh` again (`exec`, the same
process) with `PSEUDOLIFE_SHIM_ENGINE=python`, its original arguments and its
one-line reason in `PSEUDOLIFE_INSTALLER_FALLBACK`, which the wrapper prints
and records nothing for. In PowerShell it sets the global and returns, and
the wrapper carries on with the Python client. These causes qualify: no
target for the platform, a release whose canonical `SHA256SUMS` is not
published (404 with no `PSEUDOLIFE_NATIVE_ARCHIVE_URL`) or has no line
naming its archive (a line naming it in no standard checksum form refuses),
a client that does not run there, a release older than the
installer's protocol, and the tunnel check's exit 3. Each refuses as before
under an explicit `=native`, on a machine already on the native client, or
after the first host change; a defaulted run's refusals also name
`PSEUDOLIFE_SHIM_ENGINE=python`.

For the native client, the wrappers hand their arguments to
`ops/install-native.sh` / `ops/install-native.ps1`, which install the client
from the release their checkout names (`pyproject.toml`'s version) in their
native-client block, before anything on the host changes: they download that version's
`SHA256SUMS` and the archive for the host target into
`~/.pseudolife-mcp/downloads/<version>/` (kept for a rerun), refuse unless
the sums name it exactly once and its digest matches, extract the one
binary into a private temporary directory, run its `version` command, and
ask it `installer --capability`, each with a deadline (30 s;
`PSEUDOLIFE_NATIVE_PROBE_SECONDS` sets a whole number of seconds from 1 to
99999, read as decimal, and a value it cannot use is named and 30 kept), so a
client that hangs is refused rather than stalling an unattended run. It must answer exactly `pseudolife-mcp
installer-protocol <N>` with N at least the installer's level
(`NATIVE_INSTALLER_PROTOCOL`, the binary's `INSTALLER_PROTOCOL`), because the
installer runs that release's `installer` steps. A release older than the
installer (0.24.0 answers with a usage error) hands a defaulted run back to the
Python client, and under an explicit `=native` is refused, naming the route that
works today: install the Python client (the installer with
`PSEUDOLIFE_SHIM_ENGINE=python`), then move the machine with
`PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only`; the
native installer itself works from a checkout of a release that carries it. An
interactive first install stages, verifies and probes the release before its
first question, so a refusal costs no answers; an unattended one stages after
the flag checks. Any failure refuses with its cause and fix; a refusal after
the download names what it keeps for a rerun (the download, or only
`SHA256SUMS`), and nothing else changed. A `SHA256SUMS` the server does not
have (404) and one it could not serve (another HTTP status, with the status)
are told apart, and the shell installer also tells a file it could not write
(curl's write error) and a missing curl from a network failure. The staged
copy is removed however the run ends, also when the PowerShell installer
returns to an open pwsh session, and once the client is installed no later
question stages the release again. There is no
fallback to a Python client. The shim step then runs that staged binary's
`runtime install` with the verified files and registers every client
against the stable launcher; `installer migrate-registrations --client X`
moves a client's pipx, `pip --user` or Python runtime registration, and
`installer expose` adds the POSIX user-bin link. On Windows the launcher's
directory goes first on the user PATH (`ops/add-user-path.ps1`), from the
PowerShell installer and from the shell one under Git Bash alike.
`PSEUDOLIFE_NATIVE_ARCHIVE_URL` (also a local directory, for the installers
only) and `PSEUDOLIFE_NATIVE_TARGET` (one of the five targets) override the
download and the detected target. A rerun whose launcher already matches
says nothing changed.

A contributor who wants the native installer from a checkout no release
carries yet serves it archives built from that checkout: build the client
(`cargo build --release --locked -p pseudolife-stdio` in `rust/`), package it
(`python rust/ci/release_assets.py package --target <target> --binary
<the built pseudolife-stdio> --out <dir>`), write its `SHA256SUMS` (one line
per archive: the lowercase hex digest, two spaces or a space and an asterisk,
then the name, with LF line endings), and run the installer with
`PSEUDOLIFE_SHIM_ENGINE=native PSEUDOLIFE_NATIVE_ARCHIVE_URL=<dir>`. The
version in the archive's name is the checkout's `pyproject.toml` version.

- **Linux, and Git Bash on Windows:** `(cd <dir> && sha256sum
  pseudolife-mcp-*.tar.gz pseudolife-mcp-*.zip 2>/dev/null > SHA256SUMS)`.
  Git Bash's `sha256sum` writes the binary-mode form (`<digest> *<name>`),
  which the installers accept as well.
- **macOS:** `(cd <dir> && shasum -a 256 pseudolife-mcp-*.tar.gz > SHA256SUMS)`.
- **PowerShell:** set the two variables with `$env:PSEUDOLIFE_SHIM_ENGINE =
  'native'` and `$env:PSEUDOLIFE_NATIVE_ARCHIVE_URL = '<dir>'` before
  `ops\install.ps1`, and clear them after with `Remove-Item
  Env:PSEUDOLIFE_SHIM_ENGINE, Env:PSEUDOLIFE_NATIVE_ARCHIVE_URL`, since they
  stay set in that session.

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

CI prepares a real client for the installers' Python tests:
`PSEUDOLIFE_NATIVE_TEST_BINARY` names a native `pseudolife-stdio` built from
the same commit, and `PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED=1` asks a test
that needs it to fail instead of skip. CI's Python lanes that run installer
tests (`test`, `test-lite-windows`, `test-lite-macos`, the Windows nightly)
build one before their tests, smoke-run its `version` and set both
(`tests/test_ci_native_client.py` pins it, and runs the binary in the `test`
lane); on macOS that smoke run is the native client's only CI execution.
The installer tests read it (`tests/native_client_stub.py`,
`tests/test_installer_native_client.py`,
`tests/test_installer_native_client_ps.py`) and use their stand-in or skip
without it. Locally, `PSEUDOLIFE_SUITE_NATIVE_CLIENT=1` makes the suite
launchers (`ops/wsl-suite.ps1`, `ops/wsl-suite.sh`, `ops/remote-suite.ps1`)
build the client as the `test` lane does (`--locked`, the dev profile, in
WSL or on the Linux box) and set both variables. `ops/remote-suite.ps1`
turns it on by itself for an integration batch's gate
(`docs/dev/merge-batches.md` "Validating it"), and every run says
`wsl-suite: real client: yes|no` before pytest starts. It
is Linux only: the macOS and Windows lanes' runs of these tests have no
local equivalent. For a single test file, set both variables to a `cargo
build -p pseudolife-stdio` output.
