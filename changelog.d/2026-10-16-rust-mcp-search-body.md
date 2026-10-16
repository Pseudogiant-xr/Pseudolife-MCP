### Added (2026-10-16 — Native memory_search over MCP)

- The experimental Rust daemon serves `memory_search` on its MCP endpoint with the Python daemon's payload: compact entries with write dates, truncation and replacement pointers, the cortex-first block sized to the caller's `top_k` with correction calls for aged or contested facts, restated entries dropped, and the episode, source, tag and band filters. `explain` is not served yet and says so.
- The Rust `/api/search` route's unknown-band refusal now quotes band names the way Python does.
