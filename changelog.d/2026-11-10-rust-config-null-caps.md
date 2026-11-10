### Fixed (2026-11-10 — Rust daemon starts on a config that sets a cap to null, as Python does)
- A `config.yaml` that set `memory.recall.time_budget_seconds` to null (`null`, `~` or an empty value) started the Python daemon with no recall time budget, but stopped the Rust daemon at startup with "memory.recall.time_budget_seconds must be a number, got null". Twelve keys behaved this way. On each, Python turns the setting off when it is null, as it does when it is 0, while Rust refused it:
  - `memory.delete_confirm_threshold` (no bulk-delete confirmation).
  - `memory.recall.expand_budget`, `max_searches_per_hop`, `max_total_searches` and `time_budget_seconds` (no recall cap).
  - `memory.deep_dream.max_fallback_mentions` (no cap on the mention fallback scan).
  - `memory.deep_dream.auto_min_new_entities`, `auto_interval_days`, `review_queue_alert_pending` and `review_queue_alert_age_days` (that trigger or alert off).
  - `memory.lessons.synthesis_max_signals` (drain every pending signal).
  - `memory.graph_insight.betweenness_sample` (exact betweenness).
- The Rust daemon now reads a null on each of these keys as 0, which already means off (or exact) there. For the three recall knobs the Console shows, it still serves the value as null, as Python does. Any other non-number value, such as `abc`, still refuses startup with the same message as before.
