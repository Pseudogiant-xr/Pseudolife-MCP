### Fixed (2026-11-27 — extractor shim autostart works from a profile path with a curly apostrophe)
- `pseudolife-mcp extractor-shim install-autostart` on Windows now escapes all five characters PowerShell takes as a single quote (`'` and the curly `‘ ’ ‚ ‛`) in the scheduled task's code, so a path with a folder such as `O’Brien` in it (a profile or install folder) no longer breaks the task; a task already registered with plain paths is recognised as before.
- It refuses, before anything is written, any path or value it would put into the systemd unit or the scheduled task (`--env-file`, `--repo`, `--cli`, `--log`, `--native-exe`, the host, and the rest) that holds a line break or NUL, naming the setting.
- Its health check brackets an IPv6 host (`--host ::1`), on Linux and Windows.
