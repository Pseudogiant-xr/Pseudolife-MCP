//! The canonical disposable-database refusal, before any doctor diagnostics.
use std::{
    ffi::OsString,
    io::{self, Write},
    process::ExitCode,
};

const HOSTS: [&str; 4] = ["codex", "claude-code", "claude-desktop", "generic"];
const REFUSAL: &str = "{\"ok\": false, \"error\": \"ExplicitDisposableDatabaseRequired\", \"recovery\": \"Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used.\"}\n";

fn positive_decimal(raw: &str) -> bool {
    raw.bytes().any(|byte| byte.is_ascii_digit())
        && raw
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'.')
        && raw.bytes().filter(|byte| *byte == b'.').count() <= 1
        && raw
            .parse::<f64>()
            .is_ok_and(|value| value.is_finite() && value > 0.0)
}

fn parse(values: &[OsString]) -> Option<bool> {
    let mut seen = [false; 4];
    let mut values = values.iter();
    while let Some(raw) = values.next() {
        let selected = match raw.to_str()? {
            "--disposable-proof" => 0,
            "--timeout" => 1,
            "--host" => 2,
            "--agent-state" => 3,
            _ => return None,
        };
        if seen[selected] {
            return None;
        }
        seen[selected] = true;
        if selected != 0 {
            let value = values.next()?.to_str()?;
            match selected {
                1 if !positive_decimal(value) => return None,
                2 if !HOSTS.contains(&value) => return None,
                3 if value.is_empty() || value.starts_with('-') => return None,
                _ => {}
            }
        }
    }
    seen[0].then_some(seen[3])
}

pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    // Saved-state requests and nonempty database operations remain deferred.
    if parse(&values)?
        || std::env::var_os("PSEUDOLIFE_TEST_DATABASE_URL").is_some_and(|dsn| !dsn.is_empty())
    {
        return None;
    }
    Some(
        if io::stdout()
            .lock()
            .write_all(&super::text_bytes(REFUSAL))
            .is_ok()
        {
            ExitCode::from(2)
        } else {
            ExitCode::FAILURE
        },
    )
}
