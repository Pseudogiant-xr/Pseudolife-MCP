### Fixed (2026-11-17 — Connect answers supported inputs without Python)
- `connect` handles help, options before the URL, equals-form values, repeated flags and ordinary absolute directory spellings natively.
- Unsupported arguments, environment values, paths, client JSON and health responses now name their cause and remedy instead of reporting a deferral.
- DEL-bearing tokens are refused without disclosure; stdin tokens are checked before creating a directory or file. Existing registration, backup, permission and rollback protections remain in place.
