### Fixed (2026-11-05 — Report every parity judge phase after a comparison failure)
- Comparison failures no longer prevent later judge phases from running. Every phase retains a completion record and available failed case IDs, while setup and resource checks remain prerequisites and any failed or incomplete phase keeps the gate red.
