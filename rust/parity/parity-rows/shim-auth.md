| Row | Phase | Behavior to preserve and Python source | Existing test pool and boundary | Status |
|---|---:|---|---|---|
| SHIM-AUTH | 1 | Token-file precedence and reload, unsafe/malformed files fail closed, writer/session/agent/bank/principal headers; sanitized uncertain-write failures without replay. `credentials.py`, `writer_context.py`, `shim.py` | `test_shim_transport_recovery.py` mixed; `test_writer_keying.py`, `test_principals.py`, `test_credentials.py`, `test_session_identity.py` I; add wire rotation and malformed byte probes | ported |
