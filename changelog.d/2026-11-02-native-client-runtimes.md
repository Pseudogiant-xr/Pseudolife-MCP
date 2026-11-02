### Added (2026-11-02 — Opt-in native client runtimes)

- Add checksum-verified native client runtimes behind the stable launcher, with side-by-side updates, completion-marker verification and conservative process, registration and tunnel retention.
- Preserve native runtimes when using the Python compatibility updater, invalidate stale Python launcher records during native activation, and keep detached services independent of launcher lifetime.
- Reuse an identical verified archive on installer reruns while preserving explicit updater reinstallation.
- Preserve inherited Windows client-file permissions during atomic replacement for both normal and elevated users.
- Preserve active native launchers during Python compatibility and unattended updates; activate Python only with `PSEUDOLIFE_SHIM_ENGINE=python`, retain unreadable evidence and native recovery copies, and report the launcher choice on stderr.
- Stage Windows configuration replacements before publication so a blocked move leaves the original usable; verify executable digests after acquiring runtime pins without redundant pre-pin hashing.
