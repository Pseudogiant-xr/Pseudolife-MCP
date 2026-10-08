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

// The bound argparse parser has no negative-number option names. Recognized
// options precede its negative-number and literal ASCII-space value exceptions.
fn input_value(path: &OsStr) -> bool {
    let text = path.to_string_lossy();
    if text == "-" || !text.starts_with('-') {
        return true;
    }
    let flag = text.split_once('=').map_or(text.as_ref(), |(flag, _)| flag);
    let known_option = text.starts_with("-h")
        || (flag.starts_with("--")
            && ["--help", "--expect-head", "--input"]
                .iter()
                .any(|option| option.starts_with(flag)));
    !known_option && (negative_number(&text) || text.contains(' '))
}

fn negative_number(text: &str) -> bool {
    // CPython 3.11 uses Unicode 14 decimal blocks and regex $ permits one final LF.
    const ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16ac0,
        0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    let digit = |c: char| {
        ZEROS
            .iter()
            .any(|zero| (c as u32).checked_sub(*zero).is_some_and(|n| n < 10))
    };
    let text = text.strip_suffix('\n').unwrap_or(text);
    let Some(rest) = text.strip_prefix('-') else {
        return false;
    };
    (!rest.is_empty() && rest.chars().all(digit))
        || rest.split_once('.').is_some_and(|(whole, fraction)| {
            !fraction.is_empty() && whole.chars().all(digit) && fraction.chars().all(digit)
        })
}

fn deferred() -> u8 {
    crate::stderrln!(
        "board-audit: this path is deferred; only verify --input of an empty archive is implemented"
    );
    1
}
