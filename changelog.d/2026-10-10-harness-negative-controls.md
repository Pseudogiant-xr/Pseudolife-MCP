### Fixed (2026-10-10 — parity harness rejects malformed observations)
- Preserve health memory field presence and scalar kinds before normalizing readings, compare nested CLI request JSON with strict scalar types, and check listener expiry at observation and renewal time.
- Add negative controls for malformed memory readings, lease request scalar changes, expired renewed listeners, and daemon observer exceptions.
