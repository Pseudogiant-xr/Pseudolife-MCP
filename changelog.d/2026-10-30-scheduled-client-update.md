### Added (2026-10-30 — client-only machines can keep their shim on the daemon's release by themselves)
- `pseudolife-mcp update --schedule HH:MM` on a client-only machine (its
  command runs from a shim runtime and no daemon container runs there) used
  to refuse with "no daemon container to update", so nothing ever moved the
  shim on a machine whose daemon runs elsewhere, and such machines silently
  fell releases behind. It now installs that machine's own daily job, the
  task `Pseudolife Client Update` on Windows or
  `pseudolife-client-update.timer` (systemd `--user`, `Persistent=true`) on
  Linux, carrying the daemon URL. A machine that hosts the daemon keeps the
  daemon job under its old names, unchanged. Each run, `update --unattended`,
  installs the release the daemon's `/health` reports as a new shim runtime
  and refreshes the plugin cache and the Codex step, never past the daemon's
  release and never a downgrade. A daemon that does not answer changes
  nothing (exit 2), a current shim is exit 3, a held update lock exit 4, and
  every run is logged to `~/.pseudolife-mcp/unattended-update.log`. No
  bearer is needed. `--schedule` refuses there when the daemon does not
  answer, naming `--daemon-url`. `--unschedule` removes whichever job the
  machine has. A pip install that is not a shim runtime is still refused,
  and its refusal now says why.
- A client update installs only a plain release number from the daemon's
  unauthenticated `/health` (ASCII digits, `N.N` to `N.N.N.N`), in the
  scheduled run and in an attended `update --clients-only` alike; anything
  else is refused, and the attended run names `--tag <version>` to install a
  release anyway. A scheduled run that installed the shim but failed a later
  step repeats the steps at its next run instead of reporting the machine
  current, and the client timer keeps any shim-layout variables it was
  scheduled under.
