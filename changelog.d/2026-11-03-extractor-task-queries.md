### Fixed (2026-11-03 — Confirm extractor task state before replacing or removing it)

- Native Windows extractor autostart retries intermittent scheduler lookup failures within its settlement deadline instead of treating them as a missing registration. Persistent failures remain visible, and confirmed missing tasks still remove quietly.
