### Fixed (2026-11-04 — Reuse installed shim runtimes and explain the right cutover)
- Extractor autostart reuses a compatible managed Python runtime before building a venv and prints progress before dependency checks; explicit interpreter choices and incompatible-runtime fallbacks remain supported.
- Claude shim autostart now detects the daemon's Compose labels and gives stack-manager instructions for external deployments, checkout instructions only for the current checkout, and neutral guidance when no deployment can be detected.
