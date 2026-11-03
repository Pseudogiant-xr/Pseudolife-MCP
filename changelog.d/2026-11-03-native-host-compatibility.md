### Fixed (2026-11-03 — Preserve native host configuration behavior)

- The native daemon image includes the Console index and static assets and sets their serving directory.
- Native checkout builds find the adjacent plugin hooks when no explicit plugin directory is set, preserving hook-digest notices.
- Missing configured embedding artifacts now name the setting and pinned default repair. Existing local and cached ONNX graphs remain supported. Non-default reranker settings refuse startup with an actionable message instead of silently loading the pinned model.
