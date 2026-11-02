//! Test-only native argv trampoline. Never locates or runs a provider CLI.
use std::process::{Command, ExitCode};
fn main() -> ExitCode {
    let executable=std::env::current_exe().expect("fixture executable");
    let python = std::env::var_os("R6_STUB_PYTHON").unwrap_or_else(||std::fs::read_to_string(executable.with_extension("python")).expect("fixture Python").into());
    let script = std::env::var_os("R6_STUB_SCRIPT").unwrap_or_else(||std::fs::read_to_string(executable.with_extension("script")).expect("fixture script").into());
    let status = Command::new(python)
        .arg(script)
        .args(std::env::args_os().skip(1))
        .status()
        .expect("start fixture");
    ExitCode::from(status.code().unwrap_or(1) as u8)
}
