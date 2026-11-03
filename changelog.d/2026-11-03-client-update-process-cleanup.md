### Fixed (2026-11-03 — Client updater subprocess cleanup)

- Forward Unix cancellation signals to client update subprocesses and terminate remaining descendants before exiting. On Windows, retain job containment during client commands and timeouts while allowing completed commands to leave legitimate background helpers running.
