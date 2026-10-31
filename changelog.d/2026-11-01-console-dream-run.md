### Added (2026-11-01 — Run dreams from the Rust Console)

- The Rust daemon now serves `POST /api/dream/run` through the existing dream service, preserving Console limit coercion, extractor selection and the shared refusal while a dream is already running.
