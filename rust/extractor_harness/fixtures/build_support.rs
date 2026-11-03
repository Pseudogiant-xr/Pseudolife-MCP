use std::{ffi::OsStr, path::Path, process::Command};

pub fn fixture_command(
    compiler: &OsStr,
    target: &str,
    source: &Path,
    output: &Path,
    linker: Option<&OsStr>,
    encoded_rustflags: Option<&str>,
) -> Command {
    let mut command = Command::new(compiler);
    command
        .arg("--edition=2024")
        .arg("--target")
        .arg(target)
        .arg(source)
        .arg("-o")
        .arg(output);
    if let Some(linker) = linker {
        let mut option = std::ffi::OsString::from("linker=");
        option.push(linker);
        command.arg("-C").arg(option);
    }
    if let Some(flags) = encoded_rustflags.filter(|flags| !flags.is_empty()) {
        // Cargo separates arguments with unit separators, not whitespace.
        command.args(flags.split('\x1f'));
    }
    command
}
