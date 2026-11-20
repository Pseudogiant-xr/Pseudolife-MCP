### Changed (2026-11-20 — Native release assets can attach after a wheel build failure)

- GitHub releases can carry the verified native archives and Compose bundle even when the Python wheel build fails. Automatic native discovery still waits for the readiness marker after approved publication and successful image builds.
