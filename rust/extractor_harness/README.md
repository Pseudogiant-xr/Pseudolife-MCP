# Native extractor shims

The Rust CLI serves the same OpenAI-compatible completion endpoint as
`evals/claude_shim.py` and `evals/codex_shim.py`, using a locally installed,
signed-in provider CLI. This mode needs no Python at runtime.

```sh
pseudolife-mcp extractor-shim claude --cli /path/to/claude --port 8082
pseudolife-mcp extractor-shim codex --cli /path/to/codex --port 8086
```

`GET /models`, `GET /v1/models`, `GET /health`, `POST /chat/completions` and
`POST /v1/chat/completions` retain the Python response shapes. Calls are
serialized per shim. Health is warmed before binding and refreshed in the
background when stale. CLI failures become HTTP 500; an unhealthy health
probe returns HTTP 503. As in the Python shims, `stream`, `temperature` and
`response_format` do not change the completion protocol.

Concrete provider model IDs pass through; aliases such as `extractor` or `Qwen/Qwen3` keep
the launch model. Unlisted provider IDs get a one-line note. `--reasoning-effort`
sets a launch default; a nonempty request value overrides it. Claude's optional
`--emulate-tools` remains restricted to evaluation ports.
Resolved native model and effort values accept only ASCII letters, digits and `._:-`;
other characters are rejected before invoking a provider, including through
Windows batch launchers. Each unlisted model is logged once per shim process.

Both shims read the existing shared production prompt artifact,
`evals/prompts/assistant_facts_provenance.txt`; Rust embeds it at compile time.
`test_prompt.py` pins its bytes to the daemon's production prompt. A supplied
`--system-prompt-file` replaces only that prefix and preserves appended hints.
Windows Claude batch launchers use the provider's file flag to preserve
multiline system prompts; the Python oracle includes the same truncation fix.

## Configuration and autostart

`--repo /path/to/checkout` reads that checkout's `ops/.env` at each start.
`--env-file /path/to/settings.env` selects a standalone settings file. The
`PSEUDOLIFE_CLAUDE_SHIM_*` and `PSEUDOLIFE_CODEX_SHIM_*` values use the existing
runner's quoting and precedence: defaults, file values, then command flags.
Only the requested provider's settings change when writing configuration.

```sh
pseudolife-mcp extractor-shim install-autostart claude --env-file /path/to/settings.env --cli /path/to/claude
pseudolife-mcp extractor-shim install-autostart codex --env-file /path/to/settings.env --cli /path/to/codex --replace-sibling
pseudolife-mcp extractor-shim uninstall-autostart codex
```

Installation uses Task Scheduler on Windows and `systemd --user` on Linux.
Without `--repo` or `--env-file`, settings live in
`~/.pseudolife-mcp/extractor-shims.env`. Managed Claude defaults retain the
existing v5 prompt, embedded from `evals/prompts/sonnet_extractor_v5.md`.
No checkout or prompt file is required for a release installation.

Replacing a changed installation restarts the shim and verifies that its process owns
the requested listening socket. `--replace-sibling` removes the opposite
provider's known autostart registration only after the new endpoint binds.
An unrelated listener causes a refusal. Windows registration needs an
elevated shell; its default principal is the current interactive user.
`--task-logon-type S4U` explicitly selects a local-only unattended principal.
Linux uses the Docker bridge address when available; `--host` overrides it.
An unchanged, healthy registration is a quiet no-op. Installation verifies
`/health`, and a failed replacement restores the prior registration and settings.
Managed processes keep a local receipt in the user's private cache containing
a hash of canonical effective shim settings. Reinstallation checks that receipt
only after proving socket ownership; no settings fingerprint is served over HTTP.
Registration pins an explicit receipt directory in the service arguments so the
installer and scheduler can have different cache environments. Windows ownership
checks accept short and long path spellings of the same files.
Unrelated environment keys and formatting do not trigger a restart.
A disabled but healthy Linux service is re-enabled without restarting it.
Masked Linux units are refused with the command to unmask them.
If restoring a Windows password-logon task fails because its credentials are
unavailable, the original failure is preserved alongside the remaining task state.
Foreign-listener diagnostics redact credential-like command arguments.
Removing an absent Linux registration needs no service manager or Python.

The existing `install-*shim-autostart.sh` scripts accept `--native PATH`;
their PowerShell equivalents accept `-NativeExe PATH`. Their existing
Python installation paths remain available. Custom `--name` registration
names support isolated installations and tests. When installing through a versioned runtime launcher, pass its stable path with `--native-exe PATH` so future starts follow the selected runtime.

## Stub-only verification

Native timeout checks observe descendant termination and pipe reaping through a
socket witness. Separate Python-only fixture controls cover a slow correct kill
and a root-only mutation; those controls do not mutate the Rust implementation.
HTTP round-trip duration is not used as evidence that a descendant survived.
The separate lifecycle test holds health probes at a provider socket handshake,
checks cached responses while a refresh is blocked, then observes failure and
recovery after releasing each probe. Ordinary calls have a separate startup
budget from the intentional timeout condition; a blocked provider witnesses
the latter. Every lifecycle failure identifies its provider and oracle arm.
The HTTP fixture sends each request's unchanged headers and body together;
Python's early refusals can otherwise close between separate writes on Windows.
Transport errors are never retried. The native HTTP unit regression separately
withholds ignored bodies until the early response, then verifies connection reuse.

```sh
cargo test --manifest-path rust/Cargo.toml -p pseudolife-stdio --test extractor_shim
python rust/extractor_harness/test_prompt.py
PSEUDOLIFE_TEST_REAL_AUTOSTART=1 cargo test --manifest-path rust/Cargo.toml -p pseudolife-stdio --test extractor_shim extractor_shim_autostart
```

The differential harness launches both implementations with disposable stub
provider executables. It compares HTTP status and response-body bytes,
normalizing generated completion/tool IDs and the fixture CLI path in timeout
diagnostics, and compares recorded argv (with instruction-file paths normalized),
stdin bytes, instruction-file bytes and child environment. It covers routes,
models, prompt overrides, tools, errors, timeout descendant cleanup, serialized
calls, health refresh and managed configuration. Windows exercises native
executables and batch launchers separately. A dev-only fixture crate compiles
the stub and waiting launcher for Cargo's target triple and embeds their bytes
in the integration test. Nextest archives therefore need no compiler or rustup
toolchain at test time. Both differential arms receive the same stub bytes.
Python and the Rust trampolines are test fixtures only; no provider account is called.

Separate Claude and Codex autostart tests report named step timings and register
unique disposable tasks or services, testing
launch, bind-conflict refusal, replacement, config reload and listener shutdown,
including a stable launcher that waits for a runtime child on both platforms.
Windows also tests removal of detached Python tasks and startup-timeout forwarding.
Preflight (wrapper checks, mocked scheduler failures, legacy cleanup, bind
refusals and failed-install rollback) and the installed lifecycle (quiet rerun,
config reload, replacement rollback and replacement) run as separate tests for
each provider, with independent registrations and cleanup. Their cumulative
scheduler overhead must not share one 120-second nextest deadline. The direct
Python entry point retains the combined proof by default; `--scenario preflight`
and `--scenario lifecycle` select the same independent scenarios used by Cargo.
Each proof removes its registrations. Run it
only on a designated dev machine. Its Windows fixture uses S4U because an SSH
session has no interactive logon token; it does not prove an interactive logon
trigger fires. Never use it to replace an existing installation's task.
Real registrations are gated by `PSEUDOLIFE_TEST_REAL_AUTOSTART=1`, or on Windows by both
`GITHUB_ACTIONS=true` and `RUNNER_ENVIRONMENT=github-hosted` on ephemeral hosted
runners. Ordinary local and self-hosted Cargo runs skip this proof before any
scheduler action. On PowerShell, set `$env:PSEUDOLIFE_TEST_REAL_AUTOSTART='1'`
explicitly on the dev VM before the installed proof.
