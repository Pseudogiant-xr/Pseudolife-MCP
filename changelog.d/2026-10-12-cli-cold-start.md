### Performance (2026-10-12 — keep light CLI paths free of model imports)

- Configuration loading uses the MIRAS preset registry without importing the
  embedding stack. Recovery loads its client adapter only for rebind, and
  tunnel result validation loads the MCP SDK only when validating a response,
  keeping recovery/tunnel help and offline doctor checks light.
