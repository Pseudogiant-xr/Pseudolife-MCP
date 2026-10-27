### Fixed (2026-10-27 — the Rust daemon survives a PostgreSQL restart instead of failing every call until it is restarted)
- The Rust daemon opened its writer session once and never replaced it: after
  a PostgreSQL restart, a network drop or an idle-session kill, every memory
  call failed until the daemon itself was restarted. It now does what the
  Python daemon does. Before each call it checks the writer session (at most
  once a second); a dead one is replaced, the bank's writer lease is won back
  and the lease epoch is checked. If no other writer ran in the gap, the
  daemon keeps serving its resident copy. If another writer held the bank
  meanwhile, it drops the resident stores and re-reads the bank before
  serving anything.
- If another writer still holds the lease, calls are refused with the same
  message the Python daemon gives, `/health` reports `degraded` while that
  writer is there, and a retry waits 5 seconds between attempts. An
  unreachable server is retried at most every 5 seconds, as in Python.
- `rust/daemon/harness/writer_session_cases.py` runs both daemons' services
  through a killed session, a handover, a held lease and its release, and
  compares their answers, `/health` and the bank after each step.
