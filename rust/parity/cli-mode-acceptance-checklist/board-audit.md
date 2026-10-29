| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| board-audit | 2 | deferred | partly | Canonical verify, export and redact over an explicit DSN ported as CLI-AUDIT (harness row `audit`); stats (CLI-AUDIT-STATS) and the embedded/container transports (CLI-AUDIT-TRANSPORT) defer; Requested A, direct PostgreSQL/container operator path in `board_audit_cli.py`; Phase 3 reads/4 redact; `test_board_audit_cli.py::test_export_writes_json_lines_filtered_by_task_agent_and_time`, `test_verify_prints_the_head_and_fails_on_tampering_but_an_archive_still_verifies` |
