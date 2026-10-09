### Added (2026-10-09 — native read-only tunnel status and refusals)

- The experimental native `tunnel` leaf now answers `tunnel status [--json]`
  for a saved profile that has no running-process record (profile state,
  catalog, private key presence, key expiry, last refresh outcome),
  `tunnel update` when nothing is saved or every saved profile is still
  pending, and the refusals `tunnel verify` and `tunnel setup` reach before
  any write, daemon handshake, process or network use: unfinished setup,
  ambiguous daemon credentials, invalid profile names, tunnel or
  organization identifiers and key expiry, and corrupt, unsafe or missing
  profiles. Output, exit codes and files match the Python command.
- It never writes a profile, starts a process or opens a connection. Every
  other command (`setup` that would save, `doctor`, `run`, `start`, `stop`,
  `shim`, `service`), a profile with a process record, a Windows DPAPI key
  that would need unprotecting, and non-canonical arguments still defer
  before doing anything.
