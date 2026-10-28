### Changed (2026-10-29 — Rust port PRs stop colliding on the parity register)

- `rust/PARITY.md` becomes a pointer; its 475 table rows move verbatim to
  `rust/parity/<section>/<row key>.md`, one file per row under its table's
  header, and each section's prose and table headers to
  `rust/parity/<section>/_section.md`, so pull requests that change
  different rows, sections or paragraphs no longer edit the same lines.
  Three environment-appendix rows that were listed twice are listed once.
- `python rust/split_parity.py`, run during `git merge` with the branch's
  own `PARITY.md` kept, applies only that branch's changes three-way
  against the directory and restores the pointer; `--render` prints the
  whole register and `--check` verifies the layout. The contract inventory
  and the briefing hook test read the rendered register.
