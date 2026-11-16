### Fixed (2026-11-16 — a machine whose tunnel starts by itself is no longer moved to the native client) [#1632]
- A native `pseudolife-mcp update` that would move this machine's client to
  the native runtime now refuses, before it changes anything (exit 2), while
  a tunnel autostart service installed by `tunnel service install` is
  registered: a systemd unit or launchd job carrying the Pseudolife tunnel
  owner marker, or a scheduled task whose source is that marker, disabled
  ones included. The service starts the tunnel through the launcher the move
  replaces, and the native client cannot run a tunnel yet. On a disposable
  systemd user unit that loop restarted the service every 5 seconds without
  end. The refusal names the service and says how to stay on the Python
  client (update without `PSEUDOLIFE_SHIM_ENGINE=native`), and how to move
  anyway (`pseudolife-mcp tunnel service remove --profile <name>` first).
  This covers the update bridge from a Python client, `--clients-only`, a
  client machine, a release or checkout `--all` deploy, and unattended
  client updates.
- Detection only reads. On Linux and macOS it reads the unit and plist files,
  and on Windows it runs `schtasks /Query`. A host whose launcher is already
  native, or that has no registration to move, is not checked. A saved
  tunnel profile without a service still moves. When the files or the task
  list cannot be read, the update goes ahead and says what it could not
  check.
