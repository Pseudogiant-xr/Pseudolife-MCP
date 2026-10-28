## CLI-LEASE-core closure (2026-10-09)

The shared CLI differential harness verifies native `lease check`,
`lease list` and `lease run` over their canonical producer shapes. Its
[66-case row](cli_harness/rows/lease.py) and
[specification](cli_harness/specs/CLI-LEASE-core.md) compare live and golden
observations on Windows and local WSL/Linux. Exit codes, both streams,
filesystem bytes and modes, decoded HTTP JSON and intermediate coordination
table rows and sequence positions remain exact except for the three named rules in the spec:
`lease-http-json`, `lease-seeded-clock` and `lease-native-stdout`.

Seven real source mutants are caught: lock-name hashing, child status, local
lock truth, same-instance stale mirrors, timeout status, board mirror
release and the named list filter. CI's existing Parity jobs run live and platform golden comparisons
with their own disposable PostgreSQL and isolated test login. The original
Python in-process lease tests remain oracle baselines, rather than being
adapted to call a different implementation.

Hosted replay at `58f6ad58` failed on schema-catalog ordering across server
collations; `4e17a667` passed Linux but failed Windows PostgreSQL preparation.
The repaired `d5372952` passed both complete hosted Parity jobs in
[run 37932590137](https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/37932590137),
including all 66 live and golden cases on each OS. The Windows fixture uses
owned user-profile PGDATA and a standalone sanitized diagnostic. The shared
observer sorts coordination rows and sequence positions in Python; historical
seed clocks and actual lock-wait capture keep their declared bounds. This
closes the core row with the named substitutions above. Earlier observations
remain evidence for their own revisions.
CLI-LEASE's phase 4 remainder and the daemon coordination
`lease`/`release`/`leases` rows retain their separate acceptance boundaries.
No per-row performance claim or installed-candidate claim is made.
