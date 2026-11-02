### Fixed (2026-11-02 — Preserve existing YAML configuration values)

- The native daemon now reads startup configuration with Python's YAML 1.1 scalar rules, including `yes`/`no`/`on`/`off`, octal and sexagesimal numbers, and null spellings. Quoted values remain strings unless a tag requests conversion. Unquoted `off` preserves Python's behavior: literal/span gates log without enforcement, and judges are disabled. Unused metadata is ignored; unsupported timestamps, binary values and oversized integers in typed settings fail with the key, source location, value and repair.
