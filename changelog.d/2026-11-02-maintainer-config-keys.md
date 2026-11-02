### Fixed (2026-11-02 — Read-only YAML mapping keys)

- Native maintainer setup preserves distinct timestamp and binary mapping keys until filtering unrelated non-string keys, so valid configuration remains inspectable without weakening maintainer-value validation.
