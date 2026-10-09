//! `pseudolife-mcp board-audit` (board_audit_cli.py) for its canonical argv:
//! verify an export file or the bank, export the bank, redact one body.
//! The bank is reached through an explicit `PSEUDOLIFE_MCP_DATABASE_URL`;
//! `stats`, help, the lite tier's embedded instance, the daemon-container
//! transport and every other shape defer before any effect.
mod archive;
mod args;
mod bank;
mod chain;
mod codec;
mod redact;
use crate::sent_json::{self, Json};
use std::{
    ffi::OsString,
    io::{self, Write},
};

/// Run the action after `board-audit`, returning the process exit code.
pub fn run(arguments: Vec<OsString>) -> u8 {
    let Some(action) = args::parse(&arguments) else {
        return deferred();
    };
    match action {
        args::Action::VerifyInput { input, expect } => verify_input(&input, expect),
        args::Action::VerifyBank { expect } => {
            with_bank(|dsn| async move { bank::verify(&dsn, expect).await })
        }
        args::Action::Export(export) => {
            if let Some(out) = &export.out {
                // Checked before any bank is resolved, as `_export` does.
                match std::fs::metadata(out) {
                    Ok(_) => {
                        crate::stderrln!("board-audit: {out} exists; export never replaces a file");
                        return 2;
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
                        ) => {}
                    Err(_) => return deferred(),
                }
            }
            with_bank(|dsn| async move { bank::export(&dsn, &export).await })
        }
        args::Action::Redact { message_id, reason } => {
            with_bank(|dsn| async move { redact::run(&dsn, &message_id, &reason).await })
        }
    }
}

fn with_bank<F, Fut>(work: F) -> u8
where
    F: FnOnce(crate::pg::Dsn) -> Fut,
    Fut: std::future::Future<Output = bank::Ending>,
{
    let Some(dsn) = bank::dsn() else {
        return deferred();
    };
    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    else {
        return deferred();
    };
    match runtime.block_on(work(dsn)) {
        bank::Ending::Exit(code) => code,
        bank::Ending::Deferred => deferred(),
    }
}

/// Python's text-mode universal newlines: `\r\n`, `\r` and `\n` end a line.
fn python_lines(text: &str) -> impl Iterator<Item = &str> {
    let mut rest = text;
    std::iter::from_fn(move || {
        if rest.is_empty() {
            return None;
        }
        let end = rest.find(['\r', '\n']).unwrap_or(rest.len());
        let line = &rest[..end];
        let skip = if rest[end..].starts_with("\r\n") {
            2
        } else {
            usize::from(end < rest.len())
        };
        rest = &rest[end + skip..];
        Some(line)
    })
}

fn verify_input(input: &str, expect: Option<args::Head>) -> u8 {
    let bytes = match std::fs::read(input) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            crate::stderrln!("board-audit: cannot read {input}");
            return 2;
        }
        Err(_) => return deferred(),
    };
    // Python decodes in chunks and can refuse a file before reading lines
    // that precede the bad bytes, so any invalid UTF-8 defers outright.
    let Ok(text) = String::from_utf8(bytes) else {
        return deferred();
    };
    let mut walker = chain::Walker::new(expect);
    for (index, line) in python_lines(&text).enumerate() {
        if archive::blank(line) {
            continue;
        }
        let (value, seq, created_at) = match archive::row(line) {
            Ok(row) => row,
            Err(archive::Error::Deferred) => return deferred(),
            Err(error) => {
                let diagnostic = match error {
                    archive::Error::Duplicate => {
                        "has a duplicate key, so a reader and verify could see different values; it cannot be checked"
                    }
                    _ => "is not an exported audit event",
                };
                crate::stderrln!("board-audit: {input} line {} {diagnostic}", index + 1);
                return 2;
            }
        };
        match walker.push(chain::Row {
            value,
            seq,
            created_at,
        }) {
            Ok(None) => {}
            Ok(Some(broken)) => return report(broken, 1),
            Err(chain::Deferred) => return deferred(),
        }
    }
    match walker.finish() {
        Ok((report_value, intact)) => report(report_value, if intact { 0 } else { 1 }),
        Err(chain::Deferred) => deferred(),
    }
}

/// `print(json.dumps(value))`: one line on stdout, the given exit code.
fn report(value: Json, code: u8) -> u8 {
    let Ok(bytes) = sent_json::encode(&value) else {
        return deferred();
    };
    let mut text = String::from_utf8(bytes).expect("report is UTF-8");
    text.push('\n');
    if io::stdout()
        .lock()
        .write_all(&super::text_bytes(&text))
        .is_ok()
    {
        code
    } else {
        1
    }
}

fn deferred() -> u8 {
    crate::stderrln!(
        "board-audit: this path is deferred; native board-audit covers canonical verify, export and redact"
    );
    1
}

#[cfg(test)]
mod tests {
    use super::python_lines;

    #[test]
    fn universal_newlines_number_lines_as_python_does() {
        let lines: Vec<_> = python_lines("a\r\nb\rc\n\rd").collect();
        assert_eq!(lines, ["a", "b", "c", "", "d"]);
        assert_eq!(python_lines("a\n").count(), 1);
        assert_eq!(python_lines("").count(), 0);
    }
}
