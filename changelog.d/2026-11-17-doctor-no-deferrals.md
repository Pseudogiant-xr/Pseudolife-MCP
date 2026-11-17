### Fixed (2026-11-17 — Doctor always reports native diagnostics)
- `doctor` now answers help, option assignment and repeated options natively,
  accepts finite timeouts beyond the former limit, and names malformed arguments
  with a repair instruction.
- Unreadable registration files, unusual paths, client settings and daemon
  responses produce findings while independent checks continue. Private-file
  protections remain enforced, and diagnostic errors omit credential values.
