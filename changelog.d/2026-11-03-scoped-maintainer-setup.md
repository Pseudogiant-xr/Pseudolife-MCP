### Fixed (2026-11-03 — Keep post-update passkey setup in the updated project)

- Native updates now resolve the recreated daemon from the selected Compose project before checking or offering maintainer passkey setup. Both native and Python `maintainer setup --container <name>` scope inspection, configuration, key operations and restart to that container; unreadable or missing explicit targets refuse without falling back to another daemon. Calls without the option retain their existing default.
