### Fixed (2026-11-04 — Preserve daemon bearer settings during updates)
- Docker updates compare the resolved Compose bearer settings with the running daemon before pulling, backing up, tagging or building, and refuse a missing or different source with the env-file recovery command. Rollback commands preserve env-file credentials ahead of a caller's client bearer.
- An intentional credential transfer can use `--allow-auth-change` with an explicit bearer-declaring `--env-file`, `--tag`, `--reinstall` and `--daemon-only`; scheduled and checkout updates keep the default guard.
