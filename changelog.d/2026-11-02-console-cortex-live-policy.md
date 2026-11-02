### Fixed (2026-11-02 — Console cortex policy changes take effect immediately)

- Changing cortex provenance protection or the supersession confidence margin
  in the Console now updates the running cortex before the next fact write,
  without requiring a daemon restart.
