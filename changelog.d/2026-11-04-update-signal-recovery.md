### Fixed (2026-11-04 — updater cancellation recovery)

- Remove an armed shadow companion before exiting on cancellation, including streaming health responses and interrupts during failure-path removal, forward repeated Unix interrupts and quit signals to owned commands, and suspend/resume their isolated process groups with the updater.
- Preserve the remaining updater and client command timeout budget across terminal suspension, so resuming after Ctrl+Z does not time out a step solely because it was stopped.
- Forward a recovery interrupt once, without replaying the interrupt that cancelled shadow startup and accidentally forcing the cleanup command.
