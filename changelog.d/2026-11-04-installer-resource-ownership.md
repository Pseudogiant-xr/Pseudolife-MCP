### Fixed (2026-11-04 — Refuse installer collisions before changing anything)

- Both Docker installers now refuse foreign Compose containers (including stopped
  ones), unrecorded bank/state volumes, mismatched Postgres passwords and occupied
  ports before creating volumes or rewriting configuration. Refusals name the
  recovery step and preserve existing data. Recorded pre-rename volume names and
  the installer's own bound ports remain valid on reruns; extractor removal checks
  the Compose project and removes only the verified container ID.
