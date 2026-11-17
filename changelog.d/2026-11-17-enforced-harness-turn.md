### Fixed (2026-11-17 — Keep database harness runs from stopping each other's server)
- Database harness entry points share an exclusive turn on machines explicitly configured to manage their test PostgreSQL service. Contention names the live holder; other machines keep their existing behavior.
- Batch harness environments are fresh for each run and are removed only after checking the run's ownership, links, and active processes.
