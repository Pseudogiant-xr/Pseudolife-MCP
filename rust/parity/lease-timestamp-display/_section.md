## Lease timestamp display substitution

Lease timestamp display uses Chrono's platform local timezone for daemon-produced
Unix-second `expected_end` and `enqueued_at` values. Ordinary timestamps retain
local `HH:MM` on the current local date and `YYYY-MM-DD HH:MM` on other dates;
fractional seconds are floored before conversion. Unsupported calendar values
display `?`, including dates outside Chrono's representable domain, instead of
reproducing host-CRT date limits or CPython's calendar-year `OverflowError`.
JSON replies preserve the original numeric timestamps.

On POSIX, local-zone behavior includes supported `TZ` environment settings. On
Windows, the display uses the OS timezone; CRT-only `TZ` overrides are outside
this contract. Deterministic Rust timestamp fixtures inject their zone and
current date through an internal helper, without a production environment or
test switch. Public-binary fixtures cover ordinary current/other-day fractional
timestamps and unsupported-date text/JSON behavior. This substitution applies
only to these Unix-second lease fields; ISO holder timestamps retain their
existing scope.

The historical `list-expected-extreme` and `list-waiter-extreme` traceback rows
above retain their original diagnostics and receipt hashes. Their former
exception expectations are superseded for current candidates by this scoped
timestamp substitution; historical matches do not become current-head proof.
All other preparation policies and deferred lease actions remain separate.
