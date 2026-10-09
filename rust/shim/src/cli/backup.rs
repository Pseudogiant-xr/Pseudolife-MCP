//! Public backup help and explicit missing-directory refusal before bank resolution.
use std::{
    ffi::OsString,
    io::{self, Write},
    path::Path,
};

const HELP: &str = include_str!("backup_help.txt");

fn lexical_parts(value: &str, separator: char) -> String {
    value
        .split(separator)
        .filter(|part| !part.is_empty() && *part != ".")
        .collect::<Vec<_>>()
        .join(&separator.to_string())
}

// Python 3.11 pathlib removes empty/dot components but retains parents and roots.
#[cfg(not(windows))]
fn diagnostic_path(value: &str) -> String {
    let root = if value.starts_with("//") && !value.starts_with("///") {
        "//"
    } else {
        "/"
    };
    format!("{root}{}", lexical_parts(value, '/'))
}

#[cfg(windows)]
fn diagnostic_path(value: &str) -> String {
    let mut part = value.replace('/', "\\");
    let mut prefix = String::new();
    if let Some(tail) = part.strip_prefix("\\\\?\\") {
        prefix.push_str("\\\\?\\");
        part = tail.to_owned();
        if let Some(tail) = part.strip_prefix("UNC\\") {
            prefix.push_str("UNC");
            part = format!("\\\\{tail}");
        }
    }
    if let Some(after_slashes) = part.strip_prefix("\\\\")
        && !after_slashes.starts_with('\\')
        && let Some(server_end) = after_slashes.find('\\').map(|index| index + 2)
    {
        let share_separator = part[server_end + 1..]
            .find('\\')
            .map(|index| index + server_end + 1);
        if share_separator != Some(server_end + 1) {
            let share_end = share_separator.unwrap_or(part.len());
            let drive = if prefix.is_empty() {
                part[..share_end].to_owned()
            } else {
                format!("{prefix}{}", &part[1..share_end])
            };
            return format!("{drive}\\{}", lexical_parts(&part[share_end..], '\\'));
        }
    }
    let mut rest = part.as_str();
    if rest.as_bytes().get(1) == Some(&b':') && rest.as_bytes()[0].is_ascii_alphabetic() {
        prefix.push_str(&rest[..2]);
        rest = &rest[2..];
    }
    let root = if rest.starts_with('\\') { "\\" } else { "" };
    format!("{prefix}{root}{}", lexical_parts(rest, '\\'))
}

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
        diagnostic_path(value)
    );
    let _ = io::stderr().lock().write_all(&super::text_bytes(&message));
    Some(1)
}
