### Fixed (2026-11-17 — Doctor always reports native diagnostics)
- `doctor` now answers help, option assignment and repeated options natively,
  accepts finite timeouts beyond the former limit, and names malformed arguments
  with a repair instruction.
- Unreadable registration files, unusual paths, client settings and daemon
  responses produce findings while independent checks continue. Private-file
  protections remain enforced, and diagnostic errors omit credential values.
- Client registration and wake readers consistently accept a UTF-8 byte-order
  mark. Bad client files retain independent diagnostic results; HTTP error
  pages name the daemon status and repair instead of suggesting a client edit.
- Console's existing registration lookup now continues past a malformed Claude
  file to a usable Codex credential. Per-file findings are reported by doctor;
  Console keeps its existing credential-selection interface.
