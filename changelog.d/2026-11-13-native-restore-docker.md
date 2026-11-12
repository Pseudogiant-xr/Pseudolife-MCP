### Changed (2026-11-13 — native restore reads GNU long names)

- The native `pseudolife-mcp restore` now reads state archives whose members
  carry GNU long names or links, which GNU tar writes for any path over 100
  bytes, exactly as the Python command does, instead of deferring them.
