# Native client updates

Native update steps refresh supported client integrations without invoking an
interpreter, while preserving running sessions and user approvals.

Claude plugin refresh uses the client's marketplace/update commands, preserving
project scope and running caches. A failed marketplace refresh cannot make a
matching stale cache look current. Legacy GitHub-source migration backs up
settings and records, changes only this marketplace, restores only its settings
entry on refusal, and keeps HTTPS settings if the CLI moved its record before
failing. A user-selected source is retained. No update uninstalls a plugin.

Codex refresh compares script digests separately from approved definitions. It
normalizes only the documented SessionEnd/Interrupt timeout clamp, rejects
ambiguous definitions, keeps disabled handlers and never writes an approval.
Automatic marketplace refresh first reads the offered definition from a private
temporary clone and runs only when it matches the installed approved definition.
Manual launcher bundles are verified and refreshed without changing the
approved commands; old bundles remain available to running sessions. A changed
definition is reported with Codex `/hooks` approval and verification steps.

Extractor autostart inspection is informational and never registers, enables or
restarts a task. Saved tunnel refresh runs through the installed native launcher
after the shim step, never through the updating process's old code; a failed shim
step preserves the running tunnel runtime.

## Integration API

```rust,ignore
clients::run_steps(
    steps: &[&str], repo: Option<&Path>, source: &str,
    daemon_digest: Option<&str>, reinstall: bool,
) -> Value
clients::codex_reapproval_text(report: Option<&Value>) -> String
```

The client ladder returns per-step `{state, detail}` values plus `ok` and extra
runtime/migration/pruning evidence. Failed client steps remain failures for the
parent update transaction's exit handling. New approval requests are reported;
they are not silently approved. Native PATH registration remains the native
installer's responsibility on Windows; updates use absolute registered paths.

The shared consent fixtures in `rust/cli_harness/fixtures/native_codex_consent.json`
are checked by both the Python oracle and the native unit tests.
