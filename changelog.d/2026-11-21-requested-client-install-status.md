### Fixed (2026-11-21 — Installer status when requested client setup is incomplete)
- The native installers finish the daemon and other client steps before exiting with status 5 when a requested client could not be set up. The last lines name the incomplete clients and their recovery steps. Detected-client and optional setup warnings keep a successful exit.
