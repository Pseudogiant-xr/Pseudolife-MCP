### Fixed (2026-10-16 — Windows parity checks stop failing on timing alone)
- The Windows daemon MCP live parity step gets fourteen minutes instead of seven; master already took 6m49s of the seven.
- The CLI mail row's digest-removal event waits out a Windows reader's open handle instead of failing with WinError 32.
