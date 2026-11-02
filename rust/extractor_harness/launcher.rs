//! Test-only stable launcher that waits for a versioned executable child.
use std::{
    env, fs,
    process::{Command, exit},
};
fn main() {
    let target = fs::read_to_string(env::current_exe().unwrap().with_extension("target")).unwrap();
    let status = Command::new(target.trim())
        .args(env::args_os().skip(1))
        .status()
        .unwrap();
    exit(status.code().unwrap_or(1));
}
