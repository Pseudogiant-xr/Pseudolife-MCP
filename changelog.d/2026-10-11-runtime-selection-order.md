### Fixed (2026-10-11 — deterministic native runtime identity)
- The native CLI selects the running runtime in Python's stable numeric sequence order, preserving directory order for equal sequences and excluding non-numeric names. Unreadable, malformed or non-object runtime markers no longer prevent a later valid match from being reported.
