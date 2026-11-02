### Fixed (2026-11-02 — Preserve existing YAML configuration values)

- The native daemon now reads startup configuration with Python's YAML 1.1 scalar rules, including `yes`/`no`/`on`/`off`, octal and sexagesimal numbers, and null spellings. Quoted values remain strings. Unsupported timestamps, binary values and oversized integers fail with a message explaining how to repair the setting.
