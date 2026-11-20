### Added (2026-11-20 — Select the original target files in a native move)
- A carried native move to an installed Linux Docker target accepts `--target-env-file` and repeatable `--target-compose-file` paths on the target. The helper checks that the files render the installed deployment before writing settings. Resume uses the original file selection.
