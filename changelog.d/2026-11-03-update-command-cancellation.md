### Fixed (2026-11-03 — Finish update command cleanup before exiting)

- Native updates forward Unix interrupt, termination and hangup signals to their
  owned command groups and finish bounded cleanup before exiting. Windows updates
  preserve background helpers after their initiating command completes, while
  timeouts and I/O failures still terminate the owned job. The PowerShell update
  wrapper discovers Python applications without importing unrelated modules.
