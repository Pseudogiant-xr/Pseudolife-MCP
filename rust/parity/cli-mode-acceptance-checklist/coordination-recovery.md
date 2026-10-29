| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| coordination-recovery | 2 | deferred | defers | Requested A, actual direct PostgreSQL via `coordination_recovery.py`/CoordinationStore; Phase 4 recovery transaction; `test_coordination_recovery.py::test_recovery_revokes_and_rebinds_private_state_without_exposing_key`, `test_rebind_state_failure_rolls_back_credential_issuance` |
