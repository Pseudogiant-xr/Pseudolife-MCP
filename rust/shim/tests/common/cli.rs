use std::{path::Path, process::Command};

pub fn native_text(text: &str) -> Vec<u8> {
    if cfg!(windows) {
        text.replace('\n', "\r\n").into_bytes()
    } else {
        text.as_bytes().to_vec()
    }
}

pub fn cleared_command(executable: &Path) -> Command {
    let mut command = Command::new(executable);
    command.env_clear();
    for name in ["SYSTEMROOT", "WINDIR"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
}
