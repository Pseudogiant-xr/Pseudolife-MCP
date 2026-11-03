### Fixed (2026-11-03 — Receive preserves later wake delivery)
- Python now matches the native client's receive-clear behavior: clearing a deferred doorbell preserves Stop-hook marker delivery and allows an identical daemon wake to ring for a later arrival. A successful receive covers current mail even when it uses a cursor or limit.
