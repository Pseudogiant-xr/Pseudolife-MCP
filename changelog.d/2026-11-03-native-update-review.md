### Fixed (2026-11-03 — Keep native updates usable across release and host changes)

- Native updates allow new Compose mounts while protecting retained data, discover releases through configured proxies, and require an implemented updater before wrapper fallback. Checkout health checks also verify that the recreated daemon is running, and unattended updates reject malformed version text.
