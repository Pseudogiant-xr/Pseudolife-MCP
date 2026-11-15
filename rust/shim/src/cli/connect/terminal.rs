//! The terminal `connect` asks through, as the oracle does: whether stdin
//! is one (`sys.stdin.isatty()`), a prompt without echo for the pairing code
//! and the token (`getpass`), and the y/N question read from stdin. The
//! pseudo-terminal cases in `rust/cli_harness/test_pair_terminal.py` drive
//! them for real, beside the oracle.
use std::io::{self, Write};
use tokio::io::AsyncBufReadExt;

/// `connect_cli.interactive()`.
pub(super) fn interactive() -> bool {
    super::procs::stdin_is_tty()
}

/// `getpass.getpass(prompt)`: the prompt, then one line read without echo.
pub(super) async fn hidden(prompt: &str) -> io::Result<String> {
    crate::cli::pairing::prompt::password(prompt).await
}

/// `connect_cli._ask`: the question on stderr, then yes only for `y`/`yes`.
/// The answer is read without blocking the runtime, so an interrupt
/// listener (`super::interruptible`) still runs while it waits.
pub(super) async fn ask(question: &str) -> bool {
    let mut stderr = io::stderr().lock();
    let _ = stderr.write_all(question.as_bytes());
    let _ = stderr.flush();
    drop(stderr);
    let mut answer = String::new();
    let _ = tokio::io::BufReader::new(tokio::io::stdin())
        .read_line(&mut answer)
        .await;
    matches!(answer.trim().to_lowercase().as_str(), "y" | "yes")
}
