### Added (2026-10-09 — native backup help and missing-directory refusal)

- The experimental native backup leaf handles `backup --help` at `COLUMNS=80`
  and refuses `backup --data-dir PATH` when a canonical absolute UTF-8 path
  does not exist, echoing it verbatim; every other spelling defers. It
  checks the filesystem before any bank resolution. Other argument forms,
  existing paths and backup creation remain deferred.
