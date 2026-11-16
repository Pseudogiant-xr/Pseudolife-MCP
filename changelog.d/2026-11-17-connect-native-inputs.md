### Fixed (2026-11-17 — Connect answers supported inputs without Python)
- `connect` handles help, options before the URL, equals-form values, repeated flags and ordinary absolute directory spellings natively.
- Unsupported arguments, environment values, paths, client JSON and health responses now name their cause and remedy instead of reporting a deferral.
- DEL-bearing tokens are refused without disclosure; stdin tokens are checked before creating a directory or file. Existing registration, backup, permission and rollback protections remain in place.
- Redirected Windows home and AppData directories on UNC shares are accepted, including ordinary drive and UNC verbatim spellings; device namespaces remain refused. Relative XDG directory settings are ignored with a notice and use their defaults.
- The installer's `write-token-file` helper also refuses DEL-bearing tokens before creating a directory or file, since those tokens cannot be sent in an HTTP header.
