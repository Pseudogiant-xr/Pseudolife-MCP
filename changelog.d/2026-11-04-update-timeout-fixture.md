### Fixed (2026-11-04 — Native update timeout test startup race)

- Give the command-tree timeout fixture a bounded startup allowance and verify descendant exit through an OS lifetime lock, replacing the delayed survival marker that raced slow Windows executable startups.
- Bound fixture teardown and isolate descendant stdio so a deliberately broken Windows job cleanup fails and releases its surviving fixture instead of hanging in pipe teardown.
