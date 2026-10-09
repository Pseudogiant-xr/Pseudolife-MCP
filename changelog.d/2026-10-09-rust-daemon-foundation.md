### Added (2026-10-09 — contract-first Rust daemon foundation)

- The experimental Rust daemon (`rust/daemon/`) now loads `config.yaml`
  and its environment as the Python daemon does, answers the full HTTP
  route table in Python's gate order (health, Console shell, hook and
  pairing routes before the bearer gate, the browser gate, body checks,
  404/405), serves a complete `/health`, initializes lazily with Python's
  retry and refusal rules, and opens a bank exactly as `PostgresStorage`
  does: the writer lease, `ensure_schema`, the relation seeds and the lease
  epoch leave the same database state. It reads stored principals like the
  Python daemon. Routes owned by later port slices answer
  `501 not_implemented`; `rust/daemon/divergences.md` lists every declared
  difference. Nothing installs or runs it yet.
- `rust/daemon/harness/` runs the Python daemon and the Rust daemon side by
  side on disposable banks and compares responses by value and database
  state after each scenario, with a mutant control and recorded goldens.
