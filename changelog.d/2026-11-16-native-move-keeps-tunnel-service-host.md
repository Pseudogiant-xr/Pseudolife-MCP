### Fixed (2026-11-16 — a machine whose tunnel starts by itself is no longer moved to the native client) [#1632]
- A native `pseudolife-mcp update` that would move this machine's client to
  the native runtime now refuses, before it changes anything (exit 2), while
  a tunnel autostart service installed by `tunnel service install` is
  registered and runs the launcher the move replaces. Such a service is a
  systemd unit (a root `--system-service` unit included) or a launchd job
  carrying the Pseudolife tunnel owner marker, or a scheduled task whose
  source is that marker; disabled ones count. The native client cannot run a
  tunnel yet. On a disposable systemd user unit the service then restarted
  every 5 seconds without end. This covers the update bridge from a Python
  client, `--clients-only`, a client machine, a release deploy (after its
  "nothing to do" check), a checkout `--all` deploy and unattended client
  updates. An unattended update on the daemon's host updates the daemon
  alone instead of refusing, and warns at each run.
- Both native installers ask the staged client first (`runtime install
  --check`, exit 3 to refuse) and stop in their preflight, before the host
  changes. `runtime install` itself refuses the same move with exit 3, and
  an installer that meets that keeps the Python client and says so.
- The refusal names the service, how to stay on the Python client (rerun
  without `PSEUDOLIFE_SHIM_ENGINE=native`) and how to move anyway (the exact
  `pseudolife-mcp tunnel service remove` command, then rerun).
- On a machine already on the native client, an update with such a service
  goes on and warns, naming the service manager's commands that remove it.
- A service made by hand (no owner marker, but its command is the launcher
  followed by `tunnel run`) gets the same kind of warning and never a
  refusal.
- Detection only reads. On Linux and macOS it reads the unit and plist
  files, and on Windows it runs `schtasks /Query`. A saved tunnel profile
  without a service, or a service that runs another program, still moves.
  When the files or the task list cannot be read, the run goes ahead and says
  what it could not check.
