### Performance (2026-10-12 — keep light CLI paths free of model imports)

- Configuration loading uses the MIRAS preset registry without importing the
  embedding stack. Recovery loads its client adapter only for rebind, and
  tunnel result validation loads the MCP SDK only when validating a response,
  keeping recovery/tunnel help and offline doctor checks light.
- Vector adapters load when a storage connection registers them, so board
  commands stay free of NumPy with the pinned pgvector 0.4.2 as well as newer
  releases.
