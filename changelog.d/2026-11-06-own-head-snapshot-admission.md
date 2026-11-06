### Fixed (2026-11-06 — Wait for own-head snapshot replay before batching)
- Batch admission waits for the exact early replay checkpoint on each PR's own head and checks it again before opening the batch. Diffs reaching no snapshot rows remain eligible without waiting for replay metadata.
- Heads blocked by budget-only conflicts retain trusted normalization and use the batch's replay; documentation accompanied only by the generated producer census follows CI's docs-only skip.
- The first batch carrying admission bootstraps older master-built plans by checking each held member before publication.
