# Install and update experience

The rules are in `CLAUDE.md` ("Install and update experience"); this page
holds the style rules the installers (`ops/install.sh`, `ops/install.ps1`),
release-mode `pseudolife-mcp update` and the standalone `connect` / `expose`
commands follow. Read it before changing what any of them asks or prints.
Checkout mode is a contributor tool and is exempt; existing paths are brought
in line as they are touched, with no retrofit sweep.

- **Detect, don't ask** (tier, local or remote daemon, Tailscale, checkout
  or release). Ask only when undetectable, or before a persistent change
  the user did not choose by running the installer (a Tailscale serve, a
  firewall rule, a program the install does not otherwise configure):
  once, y/N with a stated default, answerable by a flag or `--yes` /
  `-Yes`. The installers' existing client setup (plugin, Codex hooks,
  instruction files, autostart) is what running them means and keeps its
  per-decision flags.
- **An unavoidable prompt is plain:** one sentence on what and why, then
  the question; a step only a person can do says where to click and what
  they will see, then waits and checks it happened.
- **Rerunning is safe and quiet:** on a set-up host a step changes nothing
  and says so in one line.
- **Unattended runs never hang:** inside install or update, a step needing
  a person is skipped with one line naming the command that finishes it; a
  standalone command for that step (`connect`, `expose`) refuses, saying
  nothing changed and naming `--yes`.
- **A refusal names its fix and leaves nothing half-done** (check first or
  undo on failure, then print the one fixing command or edit).
- **Contributor-only setup stays out:** the test login is created only by
  `--test-login` / `-TestLogin` and checkout-mode update, and checkout
  builds only by checkout mode.
