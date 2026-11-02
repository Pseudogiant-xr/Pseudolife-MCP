### Added (2026-11-02 — Native audit telemetry and import initialization)

- The native operator CLI computes board audit statistics, accepts local ISO date and time arguments, and forwards audit commands through either daemon image while keeping named files on the invoking host.
- Native imports initialize and repair target banks with the daemon's existing schema plan, preserving connection guards, schema refusals and the separate schema transaction.
