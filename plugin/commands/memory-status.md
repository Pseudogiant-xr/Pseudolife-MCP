---
description: Check the Pseudolife-MCP memory daemon and report bank health
---
Report the state of the Pseudolife memory stack:

1. Fetch `http://127.0.0.1:8765/health` (curl or WebFetch). If it fails,
   report that the daemon is down and how to start it:
   `docker compose -f <clone>/ops/docker-compose.yml up -d`
   (install guide: https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart).
2. Call `memory_stats()` and summarize: `total_memories`, store occupancy
   as a capacity meter (`bands[0].size` / `capacity` under the default
   flat preset — multi-band presets get a per-band table instead), the
   `preset`, `true_drops` (non-zero means real capacity pressure —
   flag it), the reference bank (`reference_bank_size` /
   `reference_document_count`), and `communities`. Flag
   `weights_reset: true` if present — it means the store's counters
   restarted fresh. If `read_audit` is present, note its
   `entries.never_read_pct` as a coverage signal — the old-and-unread
   tail, not the young entries that are expected to be unread yet.
3. Call `memory_dream(action="status")` for consolidation state: `backlog`,
   `idle_seconds`, `would_fire`, `dream_cursor` (how far consolidation has
   got), and any pending outcome inference. Fact/lesson counts and dream
   timing live here and in the Console, not in `memory_stats()`.
4. If `/health` reports `degraded` (or any component `error`), surface the
   failing component verbatim — do not summarize it away.
5. Mention the Cortex Console for browsing: http://127.0.0.1:8765/ui/
