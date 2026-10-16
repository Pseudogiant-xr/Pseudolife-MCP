### Fixed (2026-10-16 — Windows parity checks stop failing on timing alone)
- The Windows daemon MCP live parity step gets fourteen minutes instead of seven; master already took 6m49s of the seven.
- The CLI mail row's mid-wait digest removal and digest advance wait out a Windows reader's open handle instead of failing with WinError 32 or 5.
- The CLI harness's https fixtures finish one TLS exchange before an arm connects, so a cold machine's first server handshake no longer outruns the CLI's 0.25 s health probe (`briefing-https-hook-json` on Windows).
