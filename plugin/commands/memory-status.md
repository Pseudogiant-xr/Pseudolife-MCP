---
description: Check the Pseudolife-MCP memory daemon and report bank health
---
Report the state of the Pseudolife memory stack:

1. Fetch `http://127.0.0.1:8765/health` (curl or WebFetch). If it fails,
   report that the daemon is down and how to start it:
   `docker compose -f <clone>/ops/docker-compose.yml up -d`
   (install guide: https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart).
2. Call `memory_stats()` and summarize: total memories, band occupancy,
   cortex fact count, lessons count, last dream time.
3. If `/health` reports `degraded` (or any component `error`), surface the
   failing component verbatim — do not summarize it away.
4. Mention the Cortex Console for browsing: http://127.0.0.1:8765/ui/
