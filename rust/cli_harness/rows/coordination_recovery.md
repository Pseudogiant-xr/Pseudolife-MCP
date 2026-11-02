# CLI-COORDINATION-RECOVERY

Both arms execute `coordination-recovery recover|rebind` directly. The oracle
is `pseudolife_memory/coordination_recovery.py`; the native leaf never invokes
Python, starts a daemon or PostgreSQL instance, or migrates a restored bank.

The bank-free cases cover help, default-enabled coordination, both required
confirmations, incompatible recover/rebind options, existing private files,
Git worktree rejection, and retention of the reservation after a failed
connection. Linux goldens were recorded from the verified oracle tree at `2c770c4d`
and all nine replayed successfully. No expectations come from the candidate.
The Windows recording is being refreshed from the same bound oracle.

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

The Linux native build and all 20 live cases passed, including restored-bank
rebind and reservation replacement. Native Windows contract tests passed. A Python-free Rust image proof recovered
and rebound a stopped bank while retaining pending mail; Windows live replay
and hosted stress remain pending.
Windows stress selection: `row:coordination_recovery` (20 hosted runs).
