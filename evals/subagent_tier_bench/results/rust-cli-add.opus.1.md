The shim uses **hand-written dispatch, not clap.** `rust/shim/Cargo.toml` has no clap dependency. Modes are picked by string comparison on `args_os()`, in two layers.

**Files to change to add a subcommand** (paths under `<repo>\rust\shim\src\`):
1. **Module list:** `cli.rs:6-24`. Modules called from `main.rs` are `pub` (`pub mod lease;` at :15, `pub mod wait_mail;` at :24). Modules only called from `cli::dispatch` are private. The code itself goes in `cli/<name>.rs` or `cli/<name>/mod.rs`.
2. **Dispatch:** there are two places:
   - `main.rs:18-47` runs before the general dispatcher. This is where wait-mail, board-audit, maintainer, lease and serve are handled.
   - `cli.rs:104-181` (`pub fn dispatch`) holds the `if mode == "..." && let Some(code) = x::run(...)` chain for backup, doctor, connect, tunnel and others.
3. **Deferred-modes list:** `cli.rs:27-45` (`DEFERRED_MODES`). Modes listed here print "mode X is deferred in this candidate" and exit 1 (`cli.rs:182-189`). Anything not handled and not listed exits 2 as an unknown mode. Remove a mode from the list once it is ported, or add it if it is stubbed.
4. **Help text:** `cli_help.txt`, which `cli.rs:26` loads with `include_str!`. The `lease` entry is at `cli_help.txt:71-77`.

**Model to copy: `lease`.** In `main.rs:33-44`, it checks `Some("lease")`, converts the remaining arguments to `Vec<String>` (non-Unicode gives the stderr message "lease: arguments must be valid Unicode" and exit 2), then calls `std::process::exit(run_lease(..))`. `run_lease` (`main.rs:70-73`) is a `#[tokio::main(flavor = "current_thread")]` wrapper around `pseudolife_stdio::cli::lease::main(arguments).await`. The module is a directory, `cli/lease/` (mod.rs, args.rs, run.rs, hold.rs, view.rs, …). `lease` is not in `DEFERRED_MODES`.

**`wait-mail` is already ported**, in `cli/wait_mail.rs`. Supporting files are `wait_mail_args.rs`, `wait_mail_help.txt` and the reduction tests. It is dispatched twice: at `main.rs:18-22`, which always returns, and again at `cli.rs:111-115`. The second branch is redundant.

**What pins the Rust help text to Python:** two byte-equality tests compare `cli_help.txt` against the `_USAGE` literal in `pseudolife_memory/cli.py`.
- `evals/rust_port/test_episode_executable.py:131-137` compares against the current `cli.py`.
- `evals/rust_port/test_cli_dispatch_lane.py:193-207` (`pinned_usage`) compares against `cli.py` at commit `f709abb54f7912ae9cd767998d0926ca33df4bcd`.

So new help text must be added to Python `_USAGE` first and copied byte-for-byte. The second test is pinned to an old commit, so it will fail on any help change until that SHA is updated.

`rust/test_producer_census.py:244,278` also checks specific strings in `cli_help.txt`.
