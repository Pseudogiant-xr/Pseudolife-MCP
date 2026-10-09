//! `pseudolife-mcp pair <daemon-url> [<code> | --read-code] [--token-file
//! PATH] [--json]` (pair_cli.py). Canonical argv: the URL first, the code
//! (when given) right after it, then options, each at most once, a value as
//! the next argument. An `https` URL, environment proxies, a terminal on
//! stdin for `--read-code`, a non-ASCII code and every other argv shape
//! defer before any effect.
use super::pyjson::{self, J};
use super::token_file::{self, Failure, Moved};
use super::{Defer, net, url};
use std::path::{Path, PathBuf};
use std::time::Duration;

const EXIT_OK: u8 = 0;
const EXIT_USAGE: u8 = 2;
const EXIT_REFUSED: u8 = 4;
const EXIT_UNKNOWN: u8 = 5;
/// `RETRIES` and `BACKOFF_S`.
const RETRIES: usize = 3;
const BACKOFF_S: [u64; 3] = [1, 2, 4];
const RATE_LIMITED: &str = "pairing is rate-limited on the daemon, try again in a minute";
const ALPHABET: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

struct Args {
    url: String,
    code: Option<String>,
    read_code: bool,
    token_file: Option<String>,
    json: bool,
}

fn parse(arguments: &[String]) -> Result<Args, Defer> {
    let mut items = arguments.iter();
    let url = items.next().filter(|value| !value.starts_with('-'));
    let url = url.ok_or(Defer)?.clone();
    let mut rest: Vec<&String> = items.collect();
    let code = match rest.first() {
        Some(value) if !value.starts_with('-') => Some(rest.remove(0).clone()),
        _ => None,
    };
    let mut args = Args {
        url,
        code,
        read_code: false,
        token_file: None,
        json: false,
    };
    let mut rest = rest.into_iter();
    while let Some(option) = rest.next() {
        match option.as_str() {
            "--read-code" if !args.read_code => args.read_code = true,
            "--json" if !args.json => args.json = true,
            "--token-file" if args.token_file.is_none() => {
                let value = rest.next().filter(|value| !value.starts_with('-'));
                args.token_file = Some(value.ok_or(Defer)?.clone());
            }
            _ => return Err(Defer),
        }
    }
    Ok(args)
}

struct Report {
    url: Option<String>,
    state: &'static str,
    principal: Option<String>,
    tier: Option<String>,
    bank: Option<String>,
    token_file: Option<String>,
    warnings: Vec<String>,
    error: Option<String>,
    exit: u8,
}

impl Report {
    fn new(url: Option<&str>) -> Self {
        Self {
            url: url.map(str::to_owned),
            state: "",
            principal: None,
            tier: None,
            bank: None,
            token_file: None,
            warnings: Vec::new(),
            error: None,
            exit: 0,
        }
    }

    fn done(mut self, state: &'static str, exit: u8, error: Option<String>) -> Self {
        self.state = state;
        self.exit = exit;
        self.error = error;
        self
    }

    fn json(&self) -> J {
        let text = |value: &Option<String>| value.clone().map_or(J::Null, J::Str);
        J::Dict(vec![
            ("url".into(), text(&self.url)),
            ("state".into(), J::Str(self.state.into())),
            ("principal".into(), text(&self.principal)),
            ("tier".into(), text(&self.tier)),
            ("bank".into(), text(&self.bank)),
            ("token_file".into(), text(&self.token_file)),
            (
                "warnings".into(),
                J::List(self.warnings.iter().cloned().map(J::Str).collect()),
            ),
            ("error".into(), text(&self.error)),
            ("exit".into(), J::Int(self.exit.to_string())),
        ])
    }

    /// `_emit`.
    fn emit(&self, as_json: bool) -> u8 {
        if as_json {
            super::print(&pyjson::dumps(&self.json(), true));
            return self.exit;
        }
        for line in &self.warnings {
            super::eprint(&format!("pair: warning: {line}"));
        }
        if self.exit == EXIT_OK {
            let tier = self
                .tier
                .as_ref()
                .map_or(String::new(), |tier| format!(" (tier {tier})"));
            let principal = self
                .principal
                .as_deref()
                .unwrap_or("a principal whose name could not be used");
            super::print(&format!(
                "paired: {principal}{tier} on {}",
                self.url.as_deref().unwrap_or("None")
            ));
            super::print(&format!(
                "token file: {}",
                self.token_file.as_deref().unwrap_or("None")
            ));
        } else if let Some(error) = &self.error {
            super::eprint(&format!("pair: {error}"));
        }
        self.exit
    }
}

/// `principals.normalize_pairing_code` for ASCII text (non-ASCII defers:
/// `str.upper` can turn it into ASCII letters).
fn normalize_code(text: &str) -> Result<Option<String>, Defer> {
    if !text.is_ascii() {
        return Err(Defer);
    }
    let code: String = pyjson::py_strip(text)
        .to_ascii_uppercase()
        .chars()
        .filter(|c| *c != '-')
        .map(|c| match c {
            'O' => '0',
            'I' | 'L' => '1',
            other => other,
        })
        .collect();
    Ok((code.len() == 12 && code.chars().all(|c| ALPHABET.contains(c))).then_some(code))
}

/// `sys.stdin.readline()` on a piped stdin: up to and including the first
/// line end (universal newlines). Non-ASCII bytes defer (the text stream's
/// encoding is the locale's).
fn read_line() -> Result<String, Defer> {
    use std::io::Read;
    let mut line = String::new();
    let mut stdin = std::io::stdin().lock();
    let mut byte = [0u8; 1];
    loop {
        match stdin.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                if !byte[0].is_ascii() {
                    return Err(Defer);
                }
                if matches!(byte[0], b'\n' | b'\r') {
                    line.push('\n');
                    break;
                }
                line.push(char::from(byte[0]));
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(_) => return Err(Defer),
        }
    }
    Ok(line)
}

/// `runtimes.home()`: `USERPROFILE`, else `HOME`, as pathlib prints it.
/// A value pathlib would respell (separators, `.` parts) defers.
fn home() -> Result<PathBuf, Defer> {
    let value = ["USERPROFILE", "HOME"]
        .iter()
        .filter_map(|name| std::env::var(name).ok())
        .find(|value| !value.is_empty())
        .ok_or(Defer)?;
    let path = PathBuf::from(&value);
    let rebuilt: PathBuf = path.components().collect();
    if !path.is_absolute()
        || rebuilt.as_os_str() != path.as_os_str()
        || (cfg!(windows) && value.contains('/'))
    {
        return Err(Defer);
    }
    Ok(path)
}

fn display(path: &Path) -> Result<String, Defer> {
    path.to_str().map(str::to_owned).ok_or(Defer)
}

const KEPT: &str = "{path} is kept: the code may already be spent, and this file is the only copy of a token the daemon may now accept. Re-run `pseudolife-mcp connect <url> --token-file {path}` once the daemon answers; delete the file only if the operator re-invites this machine";

fn kept(path: &str) -> String {
    KEPT.replace("{path}", path)
}

/// `_text`: a printable string of 1..=128 characters from the daemon.
fn text(value: Option<&J>) -> Option<String> {
    let value = value?.as_str()?;
    let count = value.chars().count();
    ((1..=128).contains(&count) && value.chars().all(super::printable)).then(|| value.to_owned())
}

pub(super) async fn run(arguments: &[String]) -> Result<u8, Defer> {
    let args = parse(arguments)?;
    let usage = |line: &str, url: Option<&str>| {
        Report::new(url)
            .done("usage", EXIT_USAGE, Some(line.to_owned()))
            .emit(args.json)
    };
    let url = match url::validated(&args.url)? {
        Ok(url) => url,
        Err(()) => {
            let shown = url::shown(&args.url)?;
            return Ok(usage(
                &format!(
                    "the daemon URL given ({shown}) is not an http(s) origin: give one such as http://100.64.0.2:8765, with no path, query, fragment or credentials"
                ),
                None,
            ));
        }
    };
    if !url.starts_with("http://") || net::proxies_configured() {
        return Err(Defer);
    }
    if args.code.is_some() && args.read_code {
        return Ok(usage(
            "give the pairing code as an argument or with --read-code, not both",
            Some(&url),
        ));
    }
    if args.code.is_none() && !args.read_code {
        return Ok(usage(
            "no pairing code: give it as an argument, or pass --read-code and write it on stdin",
            Some(&url),
        ));
    }
    // Resolved before stdin is read; every deferral here is pre-effect.
    let remote = !url::is_loopback(&url)?;
    let (target, bank_directory) = match &args.token_file {
        Some(value) => (
            crate::credentials::absolute_expanded(Path::new(value)).map_err(|_| Defer)?,
            None,
        ),
        None => {
            let suffix: String = super::random_bytes(4)?
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            let directory = home()?.join(".pseudolife-mcp");
            (
                directory.join(format!("pairing-{suffix}.token")),
                Some(directory),
            )
        }
    };
    let text = if args.read_code {
        if super::stdin_is_terminal() {
            return Err(Defer);
        }
        let line = read_line()?;
        if pyjson::py_strip(&line).is_empty() {
            return Ok(usage("--read-code read no code on stdin", Some(&url)));
        }
        line
    } else {
        args.code.clone().unwrap_or_default()
    };
    let Some(code) = normalize_code(&text)? else {
        return Ok(usage(
            "that is not a pairing code: it has 12 letters and digits, shown as XXXX-XXXX-XXXX",
            Some(&url),
        ));
    };
    let mut report = redeem(&url, &code, target, bank_directory.as_deref()).await?;
    if remote {
        report.warnings.insert(0, format!("WARNING: {url} is plain HTTP: the link itself is unencrypted, so it must be a private network such as a tailnet, or a TLS reverse proxy must front the daemon."));
    }
    Ok(report.emit(args.json))
}

/// `pair_cli.redeem`.
async fn redeem(
    url: &str,
    code: &str,
    mut target: PathBuf,
    bank_directory: Option<&Path>,
) -> Result<Report, Defer> {
    let report = Report::new(Some(url));
    let shown = display(&target)?;
    if token_file::present(&target).map_err(|()| Defer)? {
        return Ok(report.done(
            "refused",
            EXIT_REFUSED,
            Some(format!(
                "{shown} already exists; pairing creates a token file but never replaces one. Nothing was changed"
            )),
        ));
    }
    // Drawn before the first request, so nothing after it can defer on it.
    let token = base64_urlsafe(&super::random_bytes(32)?);
    // Declared: past the measured nesting limit the oracle dies with an
    // uncaught RecursionError traceback (exit 1); this defers (exit 1).
    let health = net::pair_health(url).await.map_err(|_| Defer)?;
    let Some(health) =
        health.filter(|health| pyjson::get(health, "status").and_then(J::as_str) == Some("ok"))
    else {
        return Ok(report.done(
            "refused",
            EXIT_REFUSED,
            Some(format!(
                "the daemon at {url} did not answer /health with status ok; nothing was changed"
            )),
        ));
    };
    let auth = pyjson::get(&health, "auth");
    if auth != Some(&J::Bool(true)) {
        let shown_auth = pyjson::dumps_line(auth.unwrap_or(&J::Null));
        return Ok(report.done(
            "refused",
            EXIT_REFUSED,
            Some(format!(
                "the daemon at {url} does not report authentication (auth: {shown_auth}); pairing needs a daemon with a bearer token. Nothing was changed"
            )),
        ));
    }
    if let Err(failure) = token_file::write(&target, &token) {
        let error = match failure {
            Failure::Exists => format!(
                "{shown} already exists; the installer creates a token file but never replaces one; nothing was changed"
            ),
            Failure::Credential => format!(
                "CredentialError while writing {shown}; check its directory's permissions. Nothing was changed"
            ),
            Failure::Os(class) => format!(
                "{class} while writing {shown}; check its directory's permissions. Nothing was changed"
            ),
        };
        return Ok(report.done("refused", EXIT_REFUSED, Some(error)));
    }
    let mut report = report;
    report.token_file = Some(shown.clone());
    let body = format!(
        "{{\"code\": \"{code}\", \"token_sha256\": \"{}\"}}",
        super::sha256_hex(&token)
    );
    // `target.unlink(missing_ok=True)`: any error but a missing file escapes
    // the oracle as an uncaught traceback (exit 1, the file left in place).
    // Declared: this leaves the file too and exits 1 with the deferral line.
    let refused = |mut report: Report, state: &'static str, error: String| {
        match std::fs::remove_file(&target) {
            Err(problem) if problem.kind() != std::io::ErrorKind::NotFound => Err(Defer),
            _ => {
                report.token_file = None;
                Ok(report.done(state, EXIT_REFUSED, Some(error)))
            }
        }
    };
    let mut answer = None;
    let mut uncertain = false;
    for attempt in 0..=RETRIES {
        if attempt > 0 {
            tokio::time::sleep(Duration::from_secs(BACKOFF_S[(attempt - 1).min(2)])).await;
        }
        let Ok((status, payload)) = net::post_pair(url, &body).await else {
            uncertain = true;
            continue;
        };
        if status == 200
            && let Some(payload) = payload
        {
            answer = Some(payload);
            break;
        }
        if uncertain && (300..500).contains(&status) {
            let error = format!(
                "the daemon then answered HTTP {status}, after an attempt whose answer was lost. {}",
                kept(&shown)
            );
            return Ok(report.done("unknown", EXIT_UNKNOWN, Some(error)));
        }
        if status == 400 {
            return refused(report, "refused", "the daemon refused the pairing code (unknown, expired, already used or revoked); ask the operator for a new one (`pseudolife-mcp invite <machine>` on the daemon host)".to_owned());
        }
        if status == 429 {
            return refused(report, "rate_limited", RATE_LIMITED.to_owned());
        }
        if matches!(status, 401 | 404 | 405) {
            return refused(
                report,
                "refused",
                format!(
                    "the daemon does not offer pairing (HTTP {status}); update the daemon first, then re-run with the same code"
                ),
            );
        }
        if (300..500).contains(&status) {
            return refused(
                report,
                "refused",
                format!("the daemon refused the pairing request (HTTP {status})"),
            );
        }
        uncertain = true;
    }
    let Some(payload) = answer else {
        let error = format!(
            "no answer from the daemon at {url} after {} attempts. {}",
            1 + RETRIES,
            kept(&shown)
        );
        return Ok(report.done("unknown", EXIT_UNKNOWN, Some(error)));
    };
    report.tier = text(pyjson::get(&payload, "tier"));
    report.bank = text(pyjson::get(&payload, "bank"));
    let principal = pyjson::get(&payload, "principal")
        .and_then(J::as_str)
        .filter(|name| super::valid_principal_name(name) && !super::RESERVED.contains(name));
    if let Some(principal) = principal {
        report.principal = Some(principal.to_owned());
        if let Some(bank_directory) = bank_directory {
            let finale = bank_directory.join(format!("{principal}.token"));
            let final_shown = display(&finale).unwrap_or_default();
            match token_file::move_no_replace(&target, &finale) {
                Moved::Done => {
                    target = finale;
                    report.token_file = Some(final_shown);
                }
                Moved::LinkedTwice => {
                    report.warnings.push(format!(
                        "the token is in both {final_shown} and {}: the new name was linked but the pairing name could not be removed; delete {} once {final_shown} works",
                        display(&target).unwrap_or_default(),
                        display(&target).unwrap_or_default(),
                    ));
                    target = finale;
                    report.token_file = Some(final_shown);
                }
                Moved::Refused => report.warnings.push(format!(
                    "{final_shown} already exists (or could not be created); it was left as it is, and the new token stays in {}",
                    display(&target).unwrap_or_default()
                )),
            }
        }
    } else {
        let name = target
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default();
        report.warnings.push(format!(
            "the daemon returned a principal name that is not a valid file name; the token file keeps its pairing name {name}"
        ));
    }
    let path = display(&target).unwrap_or_default();
    if !net::credential_valid(url, &token).await {
        let error = format!(
            "the daemon at {url} accepted the code but refused the new token on an authenticated request. {}",
            kept(&path)
        );
        return Ok(report.done("unverified", EXIT_UNKNOWN, Some(error)));
    }
    Ok(report.done("paired", EXIT_OK, None))
}

/// `base64.urlsafe_b64encode(data).rstrip(b"=")` (`secrets.token_urlsafe`).
fn base64_urlsafe(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let bytes = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let value = (u32::from(bytes[0]) << 16) | (u32::from(bytes[1]) << 8) | u32::from(bytes[2]);
        for index in 0..=chunk.len() {
            out.push(char::from(
                TABLE[((value >> (18 - 6 * index)) & 63) as usize],
            ));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn canonical_argv_only() {
        for good in [
            &["http://h:1", "ABCD-EFGH-JKMN"][..],
            &["http://h:1", "--read-code", "--token-file", "f", "--json"],
            &["http://h:1", "c", "--json"],
            &["http://h:1"],
        ] {
            assert!(parse(&strings(good)).is_ok(), "{good:?}");
        }
        for deferred in [
            &[][..],
            &["--json", "http://h:1", "c"],
            &["http://h:1", "--json", "c"],
            &["http://h:1", "c", "d"],
            &["http://h:1", "--json", "--json"],
            &["http://h:1", "--token-file"],
            &["http://h:1", "--token-file", "-1"],
            &["http://h:1", "--token-file=f"],
            &["http://h:1", "--read"],
            &["http://h:1", "--help"],
            &["http://h:1", "--"],
        ] {
            assert!(parse(&strings(deferred)).is_err(), "{deferred:?}");
        }
    }

    #[test]
    fn codes_normalize_as_python() {
        assert_eq!(
            normalize_code(" abcd-efgh-jkmn\n").unwrap().as_deref(),
            Some("ABCDEFGHJKMN")
        );
        assert_eq!(
            normalize_code("oil0-0000-0000").unwrap().as_deref(),
            Some("011000000000")
        );
        for bad in [
            "ABCD-EFGH",
            "ABCD-EFGH-JKMU",
            "ABCD-EFGH-JKMN-P",
            "!!!!-????-****",
            "",
        ] {
            assert_eq!(normalize_code(bad).unwrap(), None, "{bad}");
        }
        assert!(normalize_code("\u{131}bcd-efgh-jkmn").is_err());
    }

    #[test]
    fn tokens_are_urlsafe_base64_without_padding() {
        assert_eq!(base64_urlsafe(&[0xfb, 0xff, 0xbf]), "-_-_");
        assert_eq!(base64_urlsafe(b"ab"), "YWI");
        assert_eq!(base64_urlsafe(&[0; 32]).len(), 43);
    }

    #[test]
    fn server_text_is_short_and_printable() {
        assert_eq!(
            text(Some(&J::Str("writer".into()))).as_deref(),
            Some("writer")
        );
        assert_eq!(text(Some(&J::Str(String::new()))), None);
        assert_eq!(text(Some(&J::Str("a\nb".into()))), None);
        assert_eq!(text(Some(&J::Str("x".repeat(129)))), None);
        assert_eq!(text(Some(&J::Int("1".into()))), None);
        assert_eq!(text(None), None);
    }
}
