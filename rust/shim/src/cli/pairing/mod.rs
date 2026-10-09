//! `pseudolife-mcp invite` (invite_cli.py) and `pseudolife-mcp pair`
//! (pair_cli.py) for their canonical argv.
//!
//! `invite` writes the bank's `principals` table over an explicit
//! `PSEUDOLIFE_MCP_DATABASE_URL`; the lite tier's embedded instance, the
//! Docker re-exec into the daemon container and the psql-in-container
//! fallback defer before any effect. `pair` mints an owner-only token here,
//! sends only its SHA-256 to `POST /api/pair` and verifies it. Every shape
//! outside the canonical ones defers before any effect: the dispatcher then
//! prints its candidate deferral line.
mod invite;
mod invite_db;
mod net;
mod pair;
mod pyjson;
mod token_file;
mod url;

use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::io::Write;
use std::process::ExitCode;

/// A shape or state this candidate does not answer; raised only before any
/// effect, so the dispatcher's deferral line is the whole outcome.
#[derive(Debug)]
pub(super) struct Defer;

/// Run `invite` or `pair`; `None` defers to the dispatcher's deferral line.
pub(super) fn run(mode: &str) -> Option<ExitCode> {
    let arguments: Vec<OsString> = std::env::args_os().skip(2).collect();
    let arguments = arguments
        .into_iter()
        .map(|argument| argument.into_string().ok())
        .collect::<Option<Vec<String>>>()?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()?;
    let code = runtime.block_on(async {
        match mode {
            "invite" => invite::run(&arguments).await,
            "pair" => pair::run(&arguments).await,
            _ => Err(Defer),
        }
    });
    code.ok().map(ExitCode::from)
}

/// `print(text)`: stdout as Python's text stream writes it. A stdout that
/// refuses the bytes is not answered differently (the work is done).
fn print(text: &str) {
    let mut line = text.to_owned();
    line.push('\n');
    let mut stdout = std::io::stdout().lock();
    let _ = stdout.write_all(&super::text_bytes(&line));
    let _ = stdout.flush();
}

/// `print(text, file=sys.stderr)`.
fn eprint(text: &str) {
    crate::stderrln!("{text}");
}

/// Python's `str.isprintable()` for one character (Unicode 14, as the
/// dispatcher's pinned repr table).
fn printable(c: char) -> bool {
    !(crate::board::claims::forbidden(c)
        || matches!(
            c,
            '\u{a0}' | '\u{1680}' | '\u{2000}'
                ..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}'
        ))
}

/// `principals.valid_principal_name`: `[a-z0-9][a-z0-9._-]{0,63}`.
fn valid_principal_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=64).contains(&bytes.len())
        && matches!(bytes[0], b'a'..=b'z' | b'0'..=b'9')
        && bytes
            .iter()
            .all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'.' | b'_' | b'-'))
}

/// `principals.RESERVED_PRINCIPALS`.
const RESERVED: [&str; 3] = ["default", "daemon", "maintainer"];

/// `principals.secret_sha256`.
fn sha256_hex(value: &str) -> String {
    Sha256::digest(value.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// `count` bytes from the operating system's CSPRNG (the TLS stack's own
/// source), as `secrets` draws from `os.urandom`.
fn random_bytes(count: usize) -> Result<Vec<u8>, Defer> {
    let mut buffer = vec![0; count];
    rustls::crypto::aws_lc_rs::default_provider()
        .secure_random
        .fill(&mut buffer)
        .map_err(|_| Defer)?;
    Ok(buffer)
}

/// Whether stdin is a terminal (`sys.stdin.isatty()`).
fn stdin_is_terminal() -> bool {
    use std::io::IsTerminal;
    std::io::stdin().is_terminal()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn principal_names_follow_the_pattern() {
        for good in ["a", "laptop", "0", "a.b_c-d", &"a".repeat(64)] {
            assert!(valid_principal_name(good), "{good}");
        }
        for bad in [
            "",
            "Laptop",
            "_x",
            ".x",
            "-x",
            "a/b",
            "box x",
            "é",
            &"a".repeat(65),
        ] {
            assert!(!valid_principal_name(bad), "{bad}");
        }
    }

    #[test]
    fn printable_matches_python() {
        for c in ['a', ' ', 'é', '\u{1f600}'] {
            assert!(printable(c), "{c:?}");
        }
        for c in [
            '\n', '\t', '\u{7f}', '\u{a0}', '\u{2028}', '\u{200b}', '\u{ffff}',
        ] {
            assert!(!printable(c), "{c:?}");
        }
    }

    #[test]
    fn digests_are_lowercase_hex() {
        assert_eq!(
            sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(random_bytes(32).unwrap().len(), 32);
    }
}
