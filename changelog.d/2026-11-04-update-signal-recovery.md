### Fixed (2026-11-04 — updater cancellation recovery)

- Remove an armed shadow companion before exiting on cancellation, forward repeated Unix interrupts and quit signals to owned commands, and suspend/resume their isolated process groups with the updater.
