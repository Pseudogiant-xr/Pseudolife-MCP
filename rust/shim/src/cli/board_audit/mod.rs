//! `pseudolife-mcp board-audit`: argparse options and help, archive or bank
//! verification, export and redaction over an explicit database URL.
//! Stats execution, embedded/container transports and local-time ISO values
//! retain their separate deferrals.
mod archive;
mod args;
mod bank;
mod chain;
mod codec;
mod help;
mod redact;
use crate::sent_json::{self, Json};
use std::{
    ffi::OsString,
    io::{self, Write},
};

/// Run the action after `board-audit`, returning the process exit code.
pub fn run(arguments: Vec<OsString>) -> u8 {
    // Deep historical payloads also recurse when dropped; use the same
    // stack allowance as the other commands using the Python JSON reader.
    std::thread::Builder::new()
        .stack_size(64 << 20)
        .spawn(move || run_inner(arguments))
        .ok()
        .and_then(|worker| worker.join().ok())
        .unwrap_or_else(deferred)
}

fn run_inner(arguments: Vec<OsString>) -> u8 {
    let Some(action) = args::parse(&arguments) else {
        return deferred();
    };
    match action {
        args::Action::Error(message) => {
            crate::stderrln!("{message}");
            2
        }
        args::Action::Help(text) => {
            let mut out = io::stdout().lock();
            if out
                .write_all(&super::text_bytes(text))
                .and_then(|()| out.flush())
                .is_ok()
            {
                0
            } else {
                120
            }
        }
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
                        ) || cfg!(unix) && error.raw_os_error() == Some(9)
                            || cfg!(target_os = "linux") && error.raw_os_error() == Some(40)
                            || cfg!(target_os = "macos") && error.raw_os_error() == Some(62)
                            || cfg!(windows)
                                && matches!(error.raw_os_error(), Some(21 | 123 | 206 | 1921)) => {}
                    Err(_) => return failed(),
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

/// TextIOWrapper decodes 8192-byte reads lazily: a bad byte in the same
/// read precedes its lines, but a later read cannot hide an earlier break.
struct PythonLines<'a> {
    bytes: &'a [u8],
    at: usize,
    tail: Vec<u8>,
    decoded: String,
}
impl<'a> PythonLines<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self {
            bytes,
            at: 0,
            tail: Vec::new(),
            decoded: String::new(),
        }
    }
    fn next(&mut self) -> Result<Option<String>, ()> {
        loop {
            if let Some(end) = self.decoded.find(['\r', '\n'])
                && (self.decoded.as_bytes()[end] != b'\r'
                    || end + 1 < self.decoded.len()
                    || self.at == self.bytes.len() && self.tail.is_empty())
            {
                let skip = if self.decoded[end..].starts_with("\r\n") {
                    2
                } else {
                    1
                };
                let line = self.decoded[..end].to_owned();
                self.decoded.drain(..end + skip);
                return Ok(Some(line));
            }
            if self.at == self.bytes.len() {
                if !self.tail.is_empty() {
                    return Err(());
                }
                return Ok((!self.decoded.is_empty()).then(|| std::mem::take(&mut self.decoded)));
            }
            let end = (self.at + 8192).min(self.bytes.len());
            self.tail.extend_from_slice(&self.bytes[self.at..end]);
            self.at = end;
            match std::str::from_utf8(&self.tail) {
                Ok(text) => {
                    self.decoded.push_str(text);
                    self.tail.clear();
                }
                Err(error) if error.error_len().is_none() => {
                    self.decoded.push_str(
                        std::str::from_utf8(&self.tail[..error.valid_up_to()]).map_err(|_| ())?,
                    );
                    self.tail.drain(..error.valid_up_to());
                }
                Err(_) => return Err(()),
            }
        }
    }
}

fn verify_input(input: &str, expect: Option<args::Head>) -> u8 {
    let bytes = match std::fs::read(input) {
        Ok(bytes) => bytes,
        Err(_) => {
            crate::stderrln!("board-audit: cannot read {input}");
            return 2;
        }
    };
    let mut lines = PythonLines::new(&bytes);
    let mut walker = chain::Walker::new(expect);
    let mut index = 0;
    loop {
        let line = match lines.next() {
            Ok(Some(line)) => line,
            Ok(None) => break,
            Err(()) => {
                crate::stderrln!(
                    "board-audit: {input} is not UTF-8 JSON lines (write exports with --out rather than a shell redirect)"
                );
                return 2;
            }
        };
        index += 1;
        if archive::blank(&line) {
            continue;
        }
        let (value, seq, created_at, pending_payload) = match archive::row(&line) {
            Ok(row) => row,
            Err(archive::Error::Deferred) => return deferred(),
            Err(archive::Error::Encoding) => return failed(),
            Err(error) => {
                let diagnostic = match error {
                    archive::Error::NotJson => "is not JSON",
                    archive::Error::Duplicate => {
                        "has a duplicate key, so a reader and verify could see different values; it cannot be checked"
                    }
                    _ => "is not an exported audit event",
                };
                crate::stderrln!("board-audit: {input} line {} {diagnostic}", index);
                return 2;
            }
        };
        match walker.push(chain::Row {
            value,
            pending_payload,
            seq,
            created_at,
        }) {
            Ok(None) => {}
            Ok(Some(broken)) => return report(broken, 1),
            Err(chain::Deferred) => return deferred(),
            Err(chain::Failure::Python) => return failed(),
        }
    }
    match walker.finish() {
        Ok((report_value, intact)) => report(report_value, if intact { 0 } else { 1 }),
        Err(chain::Deferred) => deferred(),
        Err(chain::Failure::Python) => failed(),
    }
}

/// `print(json.dumps(value))` before any effect: one line on stdout and the
/// given exit code. A stdout that refuses the line defers: CPython's answer
/// there (a buffered print, then a failed flush at interpreter shutdown,
/// exit 120 and an ignored-exception trailer) is not reproduced.
fn report(value: Json, code: u8) -> u8 {
    match print_line(&value) {
        Ok(()) => code,
        Err(()) => deferred(),
    }
}

/// One `print(json.dumps(value))` line; `Err` when stdout refused it (or the
/// value holds something the encoder refuses, which nothing here builds).
fn print_line(value: &Json) -> Result<(), ()> {
    let bytes = sent_json::encode(value).map_err(|_| ())?;
    let mut text = String::from_utf8(bytes).expect("report is UTF-8");
    text.push('\n');
    let mut stdout = io::stdout().lock();
    stdout
        .write_all(&super::text_bytes(&text))
        .and_then(|()| stdout.flush())
        .map_err(|_| ())
}

fn deferred() -> u8 {
    crate::stderrln!(
        "board-audit: this path is deferred; native board-audit covers canonical verify, export and redact"
    );
    1
}

fn failed() -> u8 {
    crate::stderrln!(
        "board-audit failed; the database error is not shown because it can include the connection string. Check PSEUDOLIFE_MCP_DATABASE_URL."
    );
    2
}

#[cfg(test)]
mod tests {
    use super::PythonLines;

    #[test]
    fn universal_newlines_number_lines_as_python_does() {
        let mut reader = PythonLines::new(b"a\r\nb\rc\n\rd");
        let mut lines = Vec::new();
        while let Some(line) = reader.next().unwrap() {
            lines.push(line);
        }
        assert_eq!(lines, ["a", "b", "c", "", "d"]);
        let mut reader = PythonLines::new(b"a\n");
        assert_eq!(reader.next().unwrap(), Some("a".into()));
        assert_eq!(reader.next().unwrap(), None);
        assert_eq!(PythonLines::new(b"").next().unwrap(), None);
    }
}
