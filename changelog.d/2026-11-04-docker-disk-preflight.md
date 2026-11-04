### Fixed (2026-11-04 — Check free space before Docker updates and backups)
- Docker updates, clone-free backups and restore safety dumps check for at least 8 GiB free on their write filesystems before pulling, building or dumping. Low-space refusals name where to free space and preserve the daemon and existing backups.
