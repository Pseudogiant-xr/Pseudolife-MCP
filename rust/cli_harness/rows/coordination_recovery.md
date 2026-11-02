# CLI-COORDINATION-RECOVERY

Both arms execute `coordination-recovery recover|rebind` directly. The oracle
is `pseudolife_memory/coordination_recovery.py`; the native leaf never invokes
Python, starts a daemon or PostgreSQL instance, or migrates a restored bank.

The bank-free cases cover help, default-enabled coordination, both required
confirmations, incompatible recover/rebind options, existing private files,
Git worktree rejection, and retention of the reservation after a failed
connection. The Windows golden is recorded from the unchanged baseline oracle;
it contains no candidate-generated expectations. Linux recording is pending.

Live cases use only owned `r1_` databases through the disposable test login.
The oracle's writers seed fixed addresses, credentials, pending mail, an active
attachment, a held lease and a waiter. Cases cover restore recovery, rebind,
ambiguous prefixes, reserved/wrong owners, invited owners with and without
board access, an unrevoked credential, repeated
recovery, pre-audit recovery, and replacement of a private-file reservation
while a database trigger holds the rebind. Each arm must validate the whole
audit chain with the oracle, retain pending mail byte-for-byte, and preserve
the generation invariant. A failed rebind must leave all rows unchanged.

`coordination-recovery-dynamic` replaces only newly appended timestamps and
their independently verified hashes, freed-lease timestamps inside the command
window, and a fresh credential after its actual private-file bytes match its
durable hash. A malformed hash, timestamp, credential, or unexercised race
fails before normalization. These cases are live-only: no invented goldens.

Integration (parent-owned shared files):

```rust
// rust/shim/src/cli.rs
pub mod coordination_recovery;
// Before the generic deferred-mode branch:
if mode == "coordination-recovery" {
    return Some(ExitCode::from(coordination_recovery::run(std::env::args_os().skip(2).collect())));
}
```

```python
# rust/cli_harness/rows/__init__.py, ROWS
"coordination_recovery": "CLI-COORDINATION-RECOVERY",
```

No dependency changes. `codec.rs` is reused from native board audit; bank
connections, private state and credential generation reuse native helpers.
The existing typed configuration boundary refuses unsupported YAML before
mutation. Canonical full option names are accepted; argparse abbreviation/error
presentation beyond the recorded cases still needs integration verification.

Validation after integration:

```text
cargo test -p pseudolife-stdio --test cli_coordination_recovery_contract
cargo test -p pseudolife-stdio cli::coordination_recovery
python rust/cli_harness --row coordination_recovery --candidate <native-binary>
python rust/cli_harness --row coordination_recovery --golden --candidate <native-binary>
```

Native build and bank cases have not been run by the implementation worker.
Windows stress selection: `row:coordination_recovery` (20 hosted runs).
