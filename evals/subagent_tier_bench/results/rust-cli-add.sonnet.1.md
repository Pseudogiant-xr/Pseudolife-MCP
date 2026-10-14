**Dispatch style:** The shim does not use clap. `rust/shim/Cargo.toml` has no clap entry. Dispatch is hand-written: `if arguments.first() == "<mode>"` blocks in `main.rs`, then `cli::dispatch(mode)` in `cli.rs`.

**Files to change for a new subcommand `foo`:**
1. `rust/shim/src/cli.rs:6-24` is the module list. It holds `mod`/`pub mod` lines in alphabetical order. Add `pub mod foo;` for a leaf that `main.rs` calls directly, or `mod foo;` for one that `dispatch` calls. The code lives in `cli/foo.rs` or `cli/foo/mod.rs`.
2. `rust/shim/src/main.rs:16-45` is the pre-dispatch chain. Use it only for leaves that bypass `cli::dispatch`. `wait-mail`, `board-audit`, `maintainer`, `lease` and `serve` are handled here.
3. `rust/shim/src/cli.rs:104-175` is `dispatch()`, which holds the other leaves. Add `if mode == "foo" && let Some(code) = foo::run(std::env::args_os().skip(2).collect()) { return Some(code); }`, as the `doctor`, `connect` and `tunnel` blocks do.
4. `rust/shim/src/cli.rs:27-45` is `DEFERRED_MODES`. If `foo` is listed there, remove it once ported. Leaving it in only affects the fallback message at lines 176-186, which says "mode ... is deferred in this candidate" with exit code 1. A mode not in the list gets "unknown mode" and exit code 2.
5. `rust/shim/src/cli_help.txt` is the help text. It is included at `cli.rs:26` through `include_str!`. `wait-mail` is described at line 67 and `lease` at lines 71-77.

**Model to copy, `lease`:** `main.rs:34-47` checks `arguments.first().and_then(|m| m.to_str()) == Some("lease")`. It rejects non-Unicode arguments with exit code 2, then calls `run_lease`. `run_lease` is a `#[tokio::main(flavor = "current_thread")]` wrapper around `pseudolife_stdio::cli::lease::main(&[String]) -> i32`. The module is `cli/lease/mod.rs`, with `args.rs`, `run.rs`, `hold.rs` and others beside it. For a synchronous command, copy the `wait-mail` shape instead: `run(Vec<OsString>) -> u8`.

**`wait-mail`:** It is already ported, in `rust/shim/src/cli/wait_mail.rs`. Its helpers are `wait_mail_args.rs`, `wait_mail_help.txt` and the `wait_mail_*_tests.rs` files. It is wired in two places. `main.rs:22-27` calls `cli::wait_mail::run`, and `cli.rs:111-115` calls it as well, which is redundant.

**What pins the help text to Python:**
- `evals/rust_port/test_cli_dispatch_lane.py:193-206` has `pinned_usage()`. It runs `git show f709abb54f7912ae9cd767998d0926ca33df4bcd:pseudolife_memory/cli.py`, takes the `_USAGE` literal with `ast`, and requires `cli_help.txt` to be byte-identical.
- The same file, lines 209-226, checks that the file stays canonical under an autocrlf checkout.
- `evals/rust_port/test_episode_executable.py:136` makes the same comparison.
- `rust/test_producer_census.py:244` and `:278` check a token string in the help text.

Editing `cli_help.txt` will break those byte-pin tests unless the pinned commit or the tests are updated too. The help text for a new subcommand therefore needs a matching change on the Python side, and the pinned hash must move to a new commit.
