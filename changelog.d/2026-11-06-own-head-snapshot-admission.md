### Fixed (2026-11-06 — Wait for own-head snapshot replay before batching)
- Batch admission waits for the exact early replay checkpoint on each PR's own head and checks it again before opening the batch. Diffs reaching no snapshot rows remain eligible without waiting for replay metadata.
