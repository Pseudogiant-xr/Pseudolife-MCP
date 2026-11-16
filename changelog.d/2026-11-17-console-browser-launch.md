### Fixed (2026-11-17 — Console browser launches explain failures without deferring)
- The native Console accepts ordinary Windows `.exe` paths in `BROWSER`, including backslashes and quoted paths with spaces. Refused values and failed launches print the sign-in link with the cause and recovery in text or JSON.
- Over SSH on macOS, the Console prints the link for local use unless `BROWSER` is explicitly set. Under WSL, Explorer's exit code 1 counts as a hand-off; other launcher failures still report their cause.
- `PYTHONINTMAXSTRDIGITS` no longer disables the native Console. Every Console harness case isolates browser lookup, including cases that should not sign in.
