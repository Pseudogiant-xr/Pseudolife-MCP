**Parser:** Hand-written. No clap in `rust/shim/Cargo.toml`. `main.rs` matches on `args_os()` strings in sequence (`main.rs:18`, `:23`, `:28`, `:33`, `:45`), then falls through to `cli::dispatch` (`cli.rs:104`).

**Files to change for a new subcommand, using `lease` as the model:**
1. **`rust/shim/src/cli.rs`**: add `pub mod <name>;` to the module list (`lease` is at `:15`, lines 6-24 in total).
2. **`rust/shim/src/main.rs`**: add an `if arguments.first()... == Some("<name>")` block modeled on `lease` at `:33-44`. That block converts argv to UTF-8 (exit 2 on failure), then calls a `#[tokio::main(flavor = "current_thread")]` wrapper (`run_lease`, `:70-73`) that calls `pseudolife_stdio::cli::<name>::main(...)`. Unlike the `wait-mail`/`board-audit` blocks, `lease` is not routed through `cli::dispatch`.
3. **`rust/shim/src/cli.rs` `DEFERRED_MODES` (`:27-45`)**: do not add the mode here unless it is a placeholder. `lease` is not in this list.
4. **`rust/shim/src/cli_help.txt`**: add a usage entry. `lease` is at lines 71-77; the file is `include_str!`'d at `cli.rs:26`.

Help-text changes also affect pins that must be updated together (see below).

**`wait-mail` is already ported**, natively, in `rust/shim/src/cli/wait_mail.rs` (about 61 KB), with `wait_mail_args.rs` and `wait_mail_help.txt` alongside it. It is wired in two places: `main.rs:18-22` (which runs first) and `cli.rs:111-115` (inside `dispatch`, effectively unreachable from the binary). The module is declared at `cli.rs:24`. Its tests are `tests/cli_wait_mail_contract.rs` and `src/cli/wait_mail_reduction_tests.rs`.

**What pins the Rust help text to Python:**
- `rust/shim/tests/cli_dispatch_contract.rs:23-27` checks the SHA-256 of the `--help` stdout against the pinned Python `cli.py::_USAGE` bytes. There are separate hashes for Windows (CRLF, `9ccc588d…`) and other platforms (`759a7ecd…`). Any help edit changes these hashes.
- `evals/rust_port/test_cli_dispatch_lane.py:193-207` extracts `_USAGE` from `pseudolife_memory/cli.py` at git commit `f709abb54f7912ae9cd767998d0926ca33df4bcd` (via `git show` and `ast`) and asserts that `cli_help.txt` is byte-equal to it. Changing the Rust help therefore means changing the Python literal and moving that commit pin; the eval test will fail otherwise.
- `rust/test_producer_census.py:244,278` also references `cli_help.txt`.
