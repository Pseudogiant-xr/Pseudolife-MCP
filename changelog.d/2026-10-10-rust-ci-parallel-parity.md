### Performance (2026-10-10 — run Rust parity groups concurrently)
- Build the release shim once per OS and reuse its workflow artifact across parallel eval, CLI and differential-judge jobs. Keep the existing Rust and Parity check names, every original command and both operating systems; the Parity gates require every dependency to succeed.
