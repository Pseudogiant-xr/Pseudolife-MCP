### Fixed (2026-11-17 — Keep database harness runs from stopping each other's server)
- Database harness entry points share an exclusive turn on machines explicitly configured to manage their test PostgreSQL service. Contention names the live holder; other machines keep their existing behavior.
- Batch harness environments are fresh for each run and are removed only after checking the run's ownership, links, and active processes.
- Batch harness commands come from the batch head's workflow coverage and candidate-dependency classifications, including candidate-free parity unit tests.
- Workflow coverage audits every job and action step, permits a readable batch head to repair an unreadable base, and reports process-query or containment failures as actionable database-turn refusals.
- Linux gate and suite dispatchers keep temporary artifacts on disk under home and refuse a memory-backed `/tmp` holding more than 4 GiB.
