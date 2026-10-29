### Security (2026-10-29 — Avoid running the daemon as root)

- The Python Docker image transfers root-owned state in `/data` to a dedicated
  account, then drops supplementary groups and root privileges before starting
  the daemon. Install and update use this entrypoint automatically, including
  existing state volumes; Compose prevents gaining new privileges and limits
  bootstrap capabilities.
