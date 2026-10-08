//! Public backup help and explicit missing-directory refusal before bank resolution.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::Path,
};

const HELP: &str = include_str!("backup_help.txt");

/// Admit only the captured COLUMNS=80 help and an explicit absolute UTF-8 path.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    if arguments == [OsString::from("--help")]
        && std::env::var_os("COLUMNS") == Some(OsString::from("80"))
    {
        return Some(
            if io::stdout()
                .lock()
                .write_all(&super::text_bytes(HELP))
                .is_ok()
            {
                0
            } else {
                1
            },
        );
    }
    let [option, value] = arguments.as_slice() else {
        return None;
    };
    if option != "--data-dir" {
        return None;
    }
    let value = value.to_str()?;
    let path = Path::new(value);
    if !path.is_absolute() || !matches!(path.try_exists(), Ok(false)) {
        return None;
    }
    let message = format!(
        "data dir {} does not exist — nothing to back up.\n",
        path.display()
    );
    let _ = io::stderr().lock().write_all(&super::text_bytes(&message));
    Some(1)
}
