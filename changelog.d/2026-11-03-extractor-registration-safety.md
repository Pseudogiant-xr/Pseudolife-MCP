### Fixed (2026-11-03 — Keep extractor registration changes scoped and reversible)

- Native Windows extractor registration refuses wildcard names and preserves unrelated tasks, including legacy and sibling name collisions. Reinstalling a healthy disabled task enables it without restarting the shim.
- A failed native Linux extractor replacement restores the previous unit enablement as well as its configuration and active state.
- Windows scheduler errors retain stop/removal context. Test cleanup requires confirmed task absence instead of accepting a failed scheduler query as proof of removal.
