### Fixed (2026-11-02 — bound CLI harness isolation relaunches)

- Preserve bytecode isolation when the CLI harness starts with Python's `-I` or `-E` flags, and refuse a second relaunch if isolation cannot be established.
