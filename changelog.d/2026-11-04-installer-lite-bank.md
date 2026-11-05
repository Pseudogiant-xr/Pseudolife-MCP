### Fixed (2026-11-04 — Preserve lite banks when installing Docker)

- Both Docker installers now refuse an initialized local lite bank before changing installer files or taking the daemon port, with the lite-to-Docker backup and restore path. Custom data directories and native Windows, macOS and Linux defaults are checked. Empty directories and binary caches remain valid fresh installs, and existing Docker installs can rerun with stopped lite files left behind.
