### Added (2026-11-14 — conductor-watch runs natively, with no Python)

- `pseudolife-mcp conductor-watch` (and its deprecated name `delegate-watch`)
  now runs its watch in the native client, in the foreground and with
  `--detach`, on Windows and Linux (macOS takes the same code, which no CI
  job builds yet). It reports the same events as
  before (pull requests opened, merged, closed, green, failing, cancelled or
  newly conflicting; master workflows failing or green again; new suite
  results; a full-suite lease going free), batched into one board message
  that rings an owner parked on "decision events".
- Each message holds at most 20 events and 6000 bytes, and says how many
  more follow in the next one. Text copied from GitHub or a suite result
  (titles, check and workflow names, summaries) is quoted, with anything
  outside printable ASCII escaped, and the message's first line says so.
- gh is looked up only in absolute `PATH` directories, never the current
  directory, so a `gh` planted in a checkout is not run.
- The watcher's own board row now shows whether it is alive. It stays
  attached while the watcher runs. Every exit, clean or not, sets its
  status to `stopped: <reason>` and detaches it. After a hard kill, the
  board shows it unattached within a minute and its status overdue within
  three polls. While gh runs, the row is still renewed and a stop request or
  an interrupt still acts.
- `--stop` still only asks: it writes the stop request only when the pid
  record names a live process with the recorded start time, and the watcher
  now notices the request within a second. Ctrl-C, Ctrl-Break, SIGTERM and
  SIGHUP end the watch the same clean way.
- `--detach` starts the native client itself, outside the caller's console,
  process group and (where allowed) job, and its caller's output closes as
  soon as it prints the watcher's board id.
- `--lease-cli` is accepted and ignored for this release (lease states are
  read natively); it will be removed later.
