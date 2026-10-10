### Fixed (2026-10-10 — CLI JSON object keys)

- Hook, episode and lease JSON readers preserve objects containing
  `$serde_json::private::Number` as ordinary data, including ignored metadata
  and episode health replies, instead of rejecting them or converting them
  into numbers.
