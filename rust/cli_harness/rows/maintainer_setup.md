# CLI-MAINTAINER-SETUP

The separate setup row keeps maintainer administration's existing harness
ownership unchanged. The Python oracle and native candidate both execute
`maintainer setup`; no Python process is used by the native implementation.

Bank-free local cases exercise help, tokenless refusal, noninteractive consent,
real configuration writes, exact original backups, invalid-name replacement,
and read-only `--check`. The Windows golden is recorded from the unchanged
baseline oracle. Linux recording is pending. Six additional live differential
cases replace Docker with an isolated program fixture: an active key, a pending
key, a stopped container, a fresh setup, restart failure, and a retry with the
name already saved but not loaded. That fixture accepts only the installed
`pseudolife-mcp maintainer setup --host-state|--host-write` command; a Python
fallback or incorrect argv fails. These cases have no recorded golden because
the baseline oracle predates the Rust-image helper transport.

`maintainer-setup-config-values` compares decoded configuration values instead
of YAML/JSON presentation and replaces a validated random backup basename.
Before normalization, the observer verifies every unrelated value, the target
name, and the backup's exact original bytes. Exit codes, diagnostics, request
fields, Docker effect order, and all other files remain compared.

Native tests additionally cover a write refused before backup creation, retry
after refusal, YAML 1.1 scalar preservation, unreadable-bank state after saving
the name, unchanged configuration on unsafe rewrites, HTTPS serve ownership,
Funnel/foreground refusal, and the distinction between host consent and the
passkey identity check. New keys are revoked on declined verification or an
interrupt at that check; a pre-existing pending key remains pending.

Integration (parent/maintainer worker owns the shared files):

```rust
// rust/shim/src/cli/maintainer/mod.rs
pub mod setup;
// At the start of run, before argument parsing or bank resolution:
if arguments.first().is_some_and(|value| value == "setup") {
    return Some(setup::run(arguments.into_iter().skip(1).collect()));
}
```

```python
# rust/cli_harness/rows/__init__.py, ROWS
"maintainer_setup": "CLI-MAINTAINER-SETUP",
```

No dependency changes. Setup reuses the native credential provider, bank
connection, PostgreSQL runtime, maintainer key-prefix, stdout and urllib-shaped
HTTP helpers. Docker calls the installed image CLI. Local bank reads reuse an
existing lite bank only; setup does not initialize a new bank or download a
runtime. The host helper supports `--host-state` and
`--host-write <rp_id> <origin>`; these are the Python oracle's Docker seam too.

Configuration output is JSON (valid YAML), preserving represented values.
Shared collection aliases, YAML merge keys, timestamps/binary/non-finite values,
non-string mapping keys and integers outside the loader domain refuse before
backup or mutation instead of losing data. This bounded rewrite domain is an
explicit remaining parity limitation; the ordinary valid-name rerun leaves
the file untouched. Complete terminal enrollment and interrupt behavior need
native Linux and Windows execution; no candidate binary was built by this
worker.

Validation after integration:

```text
cargo test -p pseudolife-stdio --test cli_maintainer_setup_contract
cargo test -p pseudolife-stdio cli::maintainer::setup
python -m pytest -q tests/test_maintainer_setup.py rust/cli_harness/test_maintainer_setup_native.py
python rust/cli_harness --row maintainer_setup --candidate <native-binary>
python rust/cli_harness --row maintainer_setup --golden --candidate <native-binary>
```

Windows stress selections: `row:maintainer_setup` and
`cargo:cli::maintainer::setup::system::tests::timeout_preserves_the_enable_link_and_reaps_its_child`
(20 hosted runs each).
