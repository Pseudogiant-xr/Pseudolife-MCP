### Added (2026-10-10 — HTTP security admission coverage)

- Added differential coverage for bearer byte encodings, duplicate headers,
  tokenless browser refusals, unavailable principal views, fail-closed token
  maps and guarded remote-bind policy, with six admission source controls.
- Preserved ordinary JSON object keys during HTTP body decoding, including
  nested objects and mixed-key objects, so valid objects reach route dispatch.
