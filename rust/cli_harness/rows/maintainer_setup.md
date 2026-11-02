# CLI-MAINTAINER-SETUP

The separate setup row keeps maintainer administration's existing harness
ownership unchanged. The Python oracle and native candidate both execute
`maintainer setup`; the native host implementation uses Rust and Docker.

Bank-free local cases exercise help, tokenless refusal, noninteractive consent,
real configuration writes, exact original backups, invalid-name replacement,
and read-only `--check`. Five invalid retained-limit cases also prove no config
or backup is written. All 30 Linux goldens were recorded under umask `022` from
the verified oracle at `7dcd738f` and replayed; all 30 Windows recordings use
the same verified oracle. Eighteen transport cases
exercise active and pending keys, stopped containers, fresh setup, restart
failure and retry against Rust, Python and unlabelled images. The fixture checks
the exact engine-specific helper command before recording any logical effect.

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

No dependency changes. Setup reuses the native credential provider, bank
connection, maintainer key-prefix, stdout and urllib-shaped
HTTP helpers. Docker selects the installed CLI for Rust images and the existing internal
Python helper for Python or unlabelled images. Native setup names and refuses
an existing lite bank before changing configuration; the lite lifecycle remains
outside this stream. The host helper supports `--host-state` and
`--host-write <rp_id> <origin>`; the Python oracle selects these only for Rust images.

Configuration output is JSON (valid YAML), preserving represented values.
Read-only state accepts safe YAML tags and timestamps in unrelated settings and
resolves merge keys with Python's precedence. Writes escape YAML-sensitive Unicode characters and
both arms validate the retained recipient limit before making a backup.
Shared collection aliases, YAML merge keys, timestamps/binary/non-finite values,
non-string mapping keys and integers outside the loader domain refuse before
backup or mutation instead of losing data. This bounded rewrite domain is an
explicit remaining parity limitation; the ordinary valid-name rerun leaves
the file untouched. Real terminal regressions passed both arms on Linux and Windows: interruption
restores the terminal, and enrolment diagnostics retain their merged stream. All 30 live cases passed on Linux and Windows, including checks,
fresh writes, restart failures and retries against all three engine labels.

Validation after integration:

```text
cargo test -p pseudolife-stdio --test cli_maintainer_setup_contract
cargo test -p pseudolife-stdio cli::maintainer::setup
python -m pytest -q tests/test_maintainer_setup.py rust/cli_harness/test_maintainer_setup_native.py
python rust/cli_harness --row maintainer_setup --candidate <native-binary>
python rust/cli_harness --row maintainer_setup --golden --candidate <native-binary>
```

Windows stress selections: `row:maintainer_setup` and
`cargo:pseudolife-stdio/lib::cli::maintainer::setup::system::tests::timeout_preserves_the_enable_link_and_reaps_its_child`
(20 hosted runs each).
