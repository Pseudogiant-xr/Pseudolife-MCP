//! First archive execution path; bank reads and nonempty chains remain deferred.
use std::{
    ffi::{OsStr, OsString},
    fs::File,
    io::{self, Read, Write},
};

const EMPTY_REPORT: &str = concat!(
    "{\"ok\": true, \"events\": 0, \"first_seq\": null, \"head_seq\": null, ",
    "\"head_hash\": null, \"head_created_at\": null, \"start_cut\": null}\n"
);

/// Verify a zero-byte archive without resolving or attaching a bank.
pub fn run(arguments: Vec<OsString>) -> u8 {
    if arguments
        .first()
        .is_some_and(|action| action == "export" || action == "stats" || action == "redact")
        || arguments == [OsString::from("verify")]
        || arguments.iter().any(|arg| arg == "--expect-head")
        || arguments.iter().any(|arg| arg == "--help" || arg == "-h")
    {
        return deferred();
    }
    let [action, option, path] = arguments.as_slice() else {
        return deferred();
    };
    if action != "verify" || option != "--input" || path.is_empty() || !input_value(path) {
        return deferred();
    }
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(_) => return deferred(),
    };
    // A nonempty archive needs the hash-chain verifier; never call it intact here.
    let mut first = [0_u8; 1];
    match file.read(&mut first) {
        Ok(0) => {}
        Ok(_) => return deferred(),
        Err(_) => return deferred(),
    }
    if io::stdout()
        .lock()
        .write_all(&super::text_bytes(EMPTY_REPORT))
        .is_ok()
    {
        0
    } else {
        1
    }
}

fn input_value(path: &OsStr) -> bool {
    path == "-" || !path.to_string_lossy().starts_with('-')
}

fn deferred() -> u8 {
    crate::stderrln!(
        "board-audit: this path is deferred; only verify --input of an empty archive is implemented"
    );
    1
}
