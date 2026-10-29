### Added (2026-10-25 — the Rust daemon serves the per-turn memory-change note)
- On the Rust daemon the plugin's UserPromptSubmit hook got a 501 for an
  authorized session, so an agent never heard that another session had
  written a status note or that a new lesson had landed. The hook is now
  served as the Python daemon serves it: line 1 is the next cursor, then a
  note when status notes from other sessions or new lessons arrived since
  the hook's last cursor (on a session's first turn, since the session
  started). The maintainer's delegate's extra reminder line is not served
  yet.
