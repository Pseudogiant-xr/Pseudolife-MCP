### Fixed (2026-11-03 — omitted retrieval limits follow configuration)

- MCP lesson search now uses `memory.lessons.top_k` when the caller omits
  `top_k`, so Console changes to the lesson limit affect the next search.
  Explicit positive limits keep their behavior, and explicit `null` is
  still rejected. The native daemon preserves its five-result default
  when the MCP argument is omitted.
- Recall now uses `memory.recall.default_hops` and `default_top_k` when
  MCP or Console callers omit those arguments. Explicit limits and
  existing invalid-argument handling retain their behavior.
