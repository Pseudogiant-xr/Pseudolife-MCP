### Fixed (2026-11-03 — Bound extractor connections and verify cleanup ownership)

- Native extractor shims close connections whose ignored request bodies do not
  finish within five seconds while retaining immediate responses and normal
  connection reuse. Windows autostart cleanup rechecks task ownership during
  deletion retries and rollback, reports scheduler query failures, and pins process handles
  before stopping captured shim processes. Test fixtures preserve compiler flags
  and avoid a second panic when cleanup fails during an existing unwind.
- Windows migration matches each historical provider's installer arguments.
  New task registrations retain the exact runtime path for cleanup across
  upgrades; ambiguous legacy processes preserve the registration and produce
  an actionable diagnostic. Process-query and termination failures explain
  what to inspect before retrying. Installation checks ambiguous runtimes before
  stopping the original shim, restarts an unchanged original after a late refusal,
  and preserves concurrent task removal during failed replacement.
