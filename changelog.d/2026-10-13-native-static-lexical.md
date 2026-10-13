### Security (2026-10-13 — Native Console static path validation)

- The native daemon candidate now validates Console static paths before filesystem access, matching the Python daemon’s lexical containment and Windows path checks while preserving in-root navigation.
