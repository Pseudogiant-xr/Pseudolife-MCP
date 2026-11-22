### Fixed (2026-11-22 — Tunnel guidance when moving to the native client)
- Explain why a registered tunnel still blocks the client switch and how to run a tunnel in the foreground afterward. For a running Python tunnel, show the retained client's stop command when its path is available.
- Refuse the native installer's guided tunnel switch before changing the host and give the explicit native profile commands. Unattended native installs with the switch now exit 1. Previously they skipped guided setup and exited 0.
