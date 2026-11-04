### Fixed (2026-11-04 — Preserve manager-owned stacks during updates)

- Release updates refuse before pulling, backing up or recreating a daemon when
its manager's compose files are unavailable on the host. The refusal explains
how to export the original stack and use `--compose-file` with `--env-file`,
preserving its project, ports, volumes and settings instead of substituting
the installer's defaults. Packaged installer layouts still support recovery
without their original checkout.
