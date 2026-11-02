### Fixed (2026-11-02 — Preserve existing YAML configuration values)

- The native daemon now reads startup configuration with Python's YAML 1.1 scalar rules, including `yes`/`no`/`on`/`off`, octal and sexagesimal numbers, and null spellings. Quoted values remain strings unless a tag requests conversion. Unquoted `off` keeps the documented dream gates disabled. Unused metadata is ignored; unsupported timestamps, binary values and oversized integers in consumed settings fail with the key, source location, value and repair.
