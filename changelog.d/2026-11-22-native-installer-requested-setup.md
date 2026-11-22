### Fixed (2026-11-22 — Report incomplete requested installer setup)
- Both native installers finish the remaining work and exit 5 if a requested client's plugin or hooks could not be set up, or shim autostart fails for an explicitly selected extractor. The completion summary names the failed step and its recovery.
- Both native installers report a linked client-only token file as an invalid file type, including when its target is empty or missing.
