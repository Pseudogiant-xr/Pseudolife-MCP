### Fixed (2026-11-02 — Native hook conductor reminder)

- The native daemon appends the conductor reminder to `memory-changes` for
  an authenticated agent with a valid operator-granted conductor lease,
  including legacy grants, matching Python's hook output.
- Reminder lookups share in-flight work, wait at most half a second, and
  cache results for at most a minute without outliving a proven lease.
- Both engines recheck a completed lookup's expiry when delivering it to a
  delayed hook request, so a descheduled waiter cannot receive an expired grant.
- Invalid non-finite lease deadlines cannot grant a reminder, and a failed
  native lookup releases its in-flight slot without breaking later hooks.
