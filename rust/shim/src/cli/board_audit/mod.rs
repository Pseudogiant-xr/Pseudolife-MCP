//! Archive register verification; bank reads and other event families defer.
mod archive;
mod codec;
use crate::sent_json::{self, Json};
use std::{
    ffi::{OsStr, OsString},
    fs::File,
    io::{self, BufRead, BufReader, Write},
};

const EMPTY_REPORT: &str = concat!(
    "{\"ok\": true, \"events\": 0, \"first_seq\": null, \"head_seq\": null, ",
    "\"head_hash\": null, \"head_created_at\": null, \"start_cut\": null}\n"
);

/// Verify an archive without resolving or attaching a bank.
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
    let file = match File::open(path) {
        Ok(file) => file,
        Err(_) => return deferred(),
    };
    let mut first_seq = None;
    let mut previous: Option<(i64, String, f64)> = None;
    let mut count = 0_u64;
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = match line {
            Ok(line) => line,
            Err(_) => return deferred(),
        };
        if archive::blank(&line) {
            continue;
        }
        let (row, seq, created_at) = match archive::row(&line) {
            Ok(row) => row,
            Err(archive::Error::Deferred) => return deferred(),
            Err(error) => {
                let diagnostic = match error {
                    archive::Error::Duplicate => {
                        "has a duplicate key, so a reader and verify could see different values; it cannot be checked"
                    }
                    _ => "is not an exported audit event",
                };
                crate::stderrln!(
                    "board-audit: {} line {} {}",
                    std::path::Path::new(path).display(),
                    index + 1,
                    diagnostic
                );
                return 2;
            }
        };
        if let Some((old_seq, old_hash, _)) = &previous {
            if old_seq.checked_add(1) != Some(seq) {
                return broken(seq, "sequence_gap");
            }
            if row["prev_hash"] != *old_hash {
                return broken(seq, "broken_link");
            }
        } else {
            first_seq = Some(seq);
            if seq == 1 && row["prev_hash"] != "0".repeat(64) {
                return broken(seq, "broken_link");
            }
        }
        // Register is the first implemented event family, not an archive rule.
        if row["event"] != "register" {
            return deferred();
        }
        let hash = match codec::hash(&row, created_at) {
            Ok(hash) => hash,
            Err(_) => return deferred(),
        };
        if row["hash"] != hash {
            return broken(seq, "hash_mismatch");
        }
        if !row["body"].is_null() || !row["body_salt"].is_null() {
            return broken(seq, "body_mismatch");
        }
        previous = Some((seq, hash, created_at));
        count += 1;
    }
    if let Some(first) = first_seq.filter(|seq| *seq != 1) {
        return broken(first, "unanchored_start");
    }
    let Some((head, hash, created_at)) = previous else {
        return write_text(EMPTY_REPORT, 0);
    };
    report(
        Json::Object(vec![
            ("ok".into(), Json::Bool(true)),
            ("events".into(), integer(count)),
            (
                "first_seq".into(),
                integer(first_seq.expect("nonempty archive")),
            ),
            ("head_seq".into(), integer(head)),
            ("head_hash".into(), Json::String(hash)),
            ("head_created_at".into(), Json::Float(created_at)),
            ("start_cut".into(), Json::Null),
        ]),
        0,
    )
}

fn integer(value: impl ToString) -> Json {
    Json::Integer(value.to_string())
}
fn broken(seq: i64, reason: &str) -> u8 {
    report(
        Json::Object(vec![
            ("ok".into(), Json::Bool(false)),
            ("seq".into(), integer(seq)),
            ("reason".into(), Json::String(reason.into())),
        ]),
        1,
    )
}
fn report(value: Json, code: u8) -> u8 {
    let Ok(bytes) = sent_json::encode(&value) else {
        return deferred();
    };
    let mut text = String::from_utf8(bytes).expect("report is UTF-8");
    text.push('\n');
    write_text(&text, code)
}
fn write_text(text: &str, code: u8) -> u8 {
    if io::stdout()
        .lock()
        .write_all(&super::text_bytes(text))
        .is_ok()
    {
        code
    } else {
        1
    }
}

fn input_value(path: &OsStr) -> bool {
    path == "-" || !path.to_string_lossy().starts_with('-')
}

fn deferred() -> u8 {
    crate::stderrln!(
        "board-audit: this path is deferred; verify --input supports empty and register archives"
    );
    1
}
