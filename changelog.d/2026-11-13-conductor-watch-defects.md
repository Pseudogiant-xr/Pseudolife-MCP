### Fixed (2026-11-13 — conductor-watch no longer crashes on odd input)

- `pseudolife-mcp conductor-watch` ended in a Python traceback, or
  misbehaved silently, on several inputs it now handles:
  - `--max-age 5ſ` (a non-ASCII letter that folds to a unit) is argparse's
    "not a duration" error, in `lease` too.
  - A `--state` with no file name (`""`, `.`, `..`) is a usage error naming
    `--state`.
  - `--settle nan` is refused; it never sent a batch.
  - A `--max-age` too large for the clock is capped at 100 years.
  - `gh` output of the wrong shape (JSON that is not a list of objects, a
    field of the wrong type, output that is not JSON) is a failed poll,
    `gh failed (n/5): gh pr list printed a JSON object, not a list of
    objects (...)`, and the watcher gives up after five in a row, as it does
    for any other `gh` failure.
  - A suite result that is JSON but not an object is skipped with one
    `skipped` ledger line.
  - A state file it cannot read gets a fresh baseline with one `reset`
    ledger line.
  - A state it cannot write is refused before the watcher registers on the
    board (exit 2, naming the file and `--state`); a write that fails later
    tells the owner.
  - An unwritable `--stop` request or `--detach` log is exit 2 with the
    reason.
  - A pid record out of range or nested too deep names no watcher.
  - On Linux, a `/proc/<pid>/stat` that is not UTF-8 is read as unreadable.
