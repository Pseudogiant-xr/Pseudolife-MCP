### Added (2026-11-02 — lossless flat-bank migration)

- Added the opt-in `pseudolife-mcp migrate-flat --apply --data-dir PATH`
  command in Python and the native CLI. It backs up a stopped bank and its
  configuration before moving entries into one flat band, preserving entry
  identities, timestamps, supersession and episode links. Capacity is retained
  or raised to fit existing entries, including superseded entries.
- Interrupted config finalization can be resumed with the same command;
  startup refuses a migrated bank with mismatched loaded config before
  hydration. The command prints both backup paths and the command to restore
  the bank and configuration together. Existing presets remain supported.
