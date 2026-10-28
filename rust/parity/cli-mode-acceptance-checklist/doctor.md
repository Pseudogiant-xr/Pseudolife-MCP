| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| doctor | 2 | deferred | partly | Read-only diagnostics ported as CLI-DOCTOR (harness row `doctor`, runtime-identity substitution); `--disposable-proof` with a database (CLI-DOCTOR-PROOF) defers; A offline/HTTP/MCP subprocess checks in `doctor_cli.py`; public `test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes`; `test_doctor_cli.py::test_doctor_hands_the_registration_credential_to_the_handshake` internal; adapter and corpus pending |
