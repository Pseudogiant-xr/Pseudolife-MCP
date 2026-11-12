### Changed (2026-11-13 — native restore answers its Docker checks)

- The native `pseudolife-mcp restore` now reads state archives whose members
  carry GNU long names or links, which GNU tar writes for any path over 100
  bytes, exactly as the Python command does, instead of deferring them.
- Confirmed with `--yes`, it now answers, as the Python command does, every
  refusal that comes before the command changes anything: another update
  holding the update lock, a container that cannot be inspected, a database
  connection that cannot be resolved, a bad port, a missing image or `/data`
  mount, a stopped PostgreSQL, no health port, a failed database probe, a
  safety directory inside a checkout or the state mount, and too little free
  space. Where the Python command would stop the daemon or start the safety
  backup, it still defers, before taking the lock or writing anything.
