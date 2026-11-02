| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| coordination-recovery | 4 | deferred | partly | Offline recover/rebind, stopped-daemon and restore confirmations, no bank startup or migration, private state persisted before commit. `shim/src/cli/coordination_recovery/`; `shim/tests/cli_coordination_recovery_contract.rs`; `cli_harness/rows/coordination_recovery.py`; baseline-oracle Windows golden recorded; Linux native build and all 20 live disposable-bank cases passed; native Windows and golden replay pending. |
