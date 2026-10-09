//! Public backup help and explicit missing-directory refusal before bank resolution.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::Path,
};

const HELP: &str = include_str!("backup_help.txt");

/// True when Python's `str(Path(value)) == value` for an absolute path, so the
/// refusal can echo the argument verbatim. Every other spelling defers.
#[cfg(not(windows))]
fn canonical_absolute(value: &str) -> bool {
    value == "/"
        || value
            .strip_prefix('/')
            .is_some_and(|rest| rest.split('/').all(|part| !part.is_empty() && part != "."))
}

#[cfg(windows)]
fn canonical_absolute(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() < 3 || !bytes[0].is_ascii_alphabetic() || &bytes[1..3] != b":\\" {
        return false;
    }
    let rest = &value[3..];
    !value.contains('/')
        && (rest.is_empty() || rest.split('\\').all(|part| !part.is_empty() && part != "."))
}

/// Admit only the captured COLUMNS=80 help and an explicit canonical absolute UTF-8 path.
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
    if !canonical_absolute(value) || !matches!(Path::new(value).try_exists(), Ok(false)) {
        return None;
    }
    let message = format!("data dir {value} does not exist — nothing to back up.\n");
    let _ = io::stderr().lock().write_all(&super::text_bytes(&message));
    Some(1)
}
