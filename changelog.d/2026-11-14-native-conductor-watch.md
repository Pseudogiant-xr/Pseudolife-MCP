### Added (2026-11-14 — conductor-watch runs natively, with no Python)

- `pseudolife-mcp conductor-watch` (and its deprecated name `delegate-watch`)
  now runs its watch in the native client, in the foreground and with
  `--detach`, on Windows and Linux (macOS takes the same code, which no CI
  job builds yet). It reports the same events as before:
  - pull requests opened, merged, closed, green, failing, cancelled or newly
    conflicting;
  - master workflows failing or green again;
  - new suite results;
  - a full-suite lease going free.

  They are batched into one board message that rings an owner parked on
  "decision events".
- **Messages:** each holds at most 20 events and 6000 bytes, and says how
  many more follow in the next one. Text copied from GitHub or a suite
  result (titles, check and workflow names, summaries) is quoted, with
  anything outside printable ASCII escaped, and the message's first line
  says so.
- **gh lookup:** only absolute `PATH` directories, never the current
  directory, so a `gh` planted in a checkout is not run.
- **One watcher per owner:** a start for an owner whose watcher already runs,
  on any state file, is answered with that watcher's board id, and `--stop`
  given the owner finds it.
- **Private files:** the watcher's state, pid, lock, log and ledger live in
  folders only this account can use, as the rest of the client's state
  does.
- **The board row:**
  - It stays attached while the watcher runs, also while gh does.
  - Every exit sets its status to `stopped: <reason>` and detaches it.
  - The owner is mailed before each exit while the board can be reached:
    max age, the `--until-lease` lease lost, an interrupt, a stop, or a
    reason to give up.
  - After a hard kill, the board shows the row unattached within a minute,
    and its status overdue within three polls.
- **`--stop`** still only asks. It writes the stop request only for a live
  process whose start time is the recorded one; when a start time cannot be
  read, the state's lock decides, so a reused pid never counts as a watcher.
  The watcher notices the request within a second, also while gh runs.
- **Interrupts:** Ctrl-C, Ctrl-Break, SIGTERM and SIGHUP end the watch the
  same clean way.
- **`--detach`:**
  - It starts the native client itself, outside the caller's console and
    process group, and outside its job where allowed.
  - The watcher runs from its state's folder, so it does not keep the
    caller's folder in use; the caller's repository is named for it.
  - It reports the watcher running only once it registered and finished
    its first poll (within 45 s), and otherwise says why it stopped.
  - Where a Windows job will end the watcher with the session, it says so.
- **`--lease-cli`** is accepted and ignored for this release (lease states
  are read natively); it will be removed later.
