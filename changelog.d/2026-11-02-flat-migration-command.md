### Added (2026-11-02 — lossless flat-bank migration)

- Added the opt-in `pseudolife-mcp migrate-flat --apply --data-dir PATH --external-recovery-verified`
  command in Python and the native CLI. It backs up a stopped bank and its
  configuration before moving entries into one flat band, preserving entry
  identities, timestamps, supersession and episode links. Capacity is retained
  or raised to fit existing entries, including superseded entries.
- Interrupted config finalization can be resumed with the same command;
  startup refuses a migrated bank with mismatched loaded config before
  hydration. The command refuses before effects unless the operator explicitly
  acknowledges a rehearsed external restore of this exact bank and config;
  it prints both backup paths and never guesses a Docker restore target.
  Existing presets remain supported.
- Migration refuses mismatched config-path overrides and resumes a pending
  marker after unrelated edits to a sufficient flat layout. Native planning
  preserves non-specific YAML tags and cannot inherit the bank's database URL.
- Migration verifies that every entry is flat before accepting a completed
  marker, names the required capacity when repairing a flat layout, and
  preserves YAML value-tag mapping keys. An incompatible native planner now
  names the matching binary needed instead of reporting a missing database URL.
