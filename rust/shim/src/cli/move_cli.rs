//! `move`: the refusals `pseudolife-mcp move` makes before any effect.
//!
//! Oracle: `pseudolife_memory/move_cli.py` (`_parser`, `main` and the first
//! two checks of `Mover.run`). This leaf prints native help and preserves
//! the established early refusals. The native transaction handles accepted
//! move arguments. This module does no I/O beyond writing its answer to
//! stdout or stderr (`tests/cli_move_contract.rs` scans it for that).
use std::{
    ffi::OsString,
    io::{self, ErrorKind, Write},
};

/// Native help, the same at every terminal width; the usage block keeps
/// argparse's layout at 78 columns.
const HELP: &str = concat!(
    "usage: pseudolife-mcp move [-h] --to SSH-TARGET [--target-checkout PATH]\n",
    "                           [--target-url URL] [--no-keep-tokens] [--resume]\n",
    "                           [--dry-run] [--expose-target] [--target-release]\n",
    "                           [--target-env-file TARGET_ENV_FILE]\n",
    "                           [--target-compose-file TARGET_COMPOSE_FILE]\n",
    "                           [--target-work-dir TARGET_WORK_DIR] [--yes]\n",
    "                           [--json]\n",
    "\n",
    "Move this host's Docker-tier bank to an installed release deployment over\n",
    "key-based ssh. The target needs the native pseudolife-mcp client. The move\n",
    "backs up and fences the stopped source, verifies the restored bank, starts\n",
    "the target and re-points this machine's clients. Exit codes: 0\n",
    "moved, 1 failed and rolled back, 2 usage or not confirmed, 4 refused before\n",
    "any change, 5 moved but a follow-up failed (the report names it), 6 failed\n",
    "and the rollback could not finish. Run it under tmux, screen or nohup.\n",
    "\n",
    "options:\n",
    "  -h, --help            show this help message and exit\n",
    "  --to SSH-TARGET       the target host as ssh takes it (user@box, or a\n",
    "                        ~/.ssh/config alias)\n",
    "  --target-checkout PATH\n",
    "                        refused: native move needs an installed release\n",
    "                        deployment on the target; omit this option\n",
    "  --target-url URL      the URL clients will use for the target; required\n",
    "                        unless you pass --expose-target\n",
    "  --no-keep-tokens      do not carry the environment identities; invite each\n",
    "                        machine on the target again\n",
    "  --resume              overwrite the half-restored bank an earlier attempt of\n",
    "                        this move left on the target\n",
    "  --dry-run             show the plan; change nothing\n",
    "  --expose-target       consent to creating a persistent Tailscale serve on\n",
    "                        the target if needed\n",
    "  --target-release      accepted for compatibility; native move always uses\n",
    "                        the target's installed release\n",
    "  --target-env-file TARGET_ENV_FILE\n",
    "                        the target deployment's original environment file,\n",
    "                        read when carrying bearer settings\n",
    "  --target-compose-file TARGET_COMPOSE_FILE\n",
    "                        an original Compose file of the target deployment;\n",
    "                        repeat for overlays, in order\n",
    "  --target-work-dir TARGET_WORK_DIR\n",
    "                        target artifact directory outside bank mounts and\n",
    "                        checkouts\n",
    "  --yes                 confirm the bank move (exposure needs --expose-target)\n",
    "  --json                one JSON report on stdout\n",
    "\n",
    "Recovery:\n",
    "  pseudolife-mcp move --rollback MOVE-ID [--yes] [--json]\n",
    "  pseudolife-mcp move --release-target MOVE-ID [--target-gone] [--yes] [--json]\n",
    "  To retry a move rolled back after its restore, rerun it with --resume.\n",
);
const REQUIRED: &str = "pseudolife-mcp move: error: the following arguments are required: --to\n";
const BAD_TO: &str = "move: --to takes an ssh target such as user@box or a ~/.ssh/config alias\n";
const BAD_URL: &str = "move: --target-url must be an http(s) origin such as http://100.64.0.2:8765 \
                       (no path, query or credentials)\n";
const BAD_DEPLOY_PATH: &str = "move: target environment, compose and work directory paths must contain no control characters\n";
/// The options that take a value, each as the next argument.
const VALUE_OPTIONS: [&str; 6] = [
    "--to",
    "--target-checkout",
    "--target-url",
    "--target-env-file",
    "--target-compose-file",
    "--target-work-dir",
];
/// The recovery forms (`move --rollback MOVE-ID`, `move --release-target MOVE-ID`).
const RECOVERY: [&str; 2] = ["--rollback", "--release-target"];
const FLAGS: [&str; 7] = [
    "--no-keep-tokens",
    "--resume",
    "--dry-run",
    "--yes",
    "--json",
    "--expose-target",
    "--target-release",
];

/// What the early refusals depend on besides argv.
pub(super) struct Context {
    /// CPython decodes this argv as UTF-8 (see `utf8_argv`).
    pub(super) utf8_argv: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Decision {
    /// One refusal on stderr, exit 2 (`EXIT_USAGE`): argparse's usage error
    /// or one of `main` / `Mover.run`'s messages.
    Refuse(String),
    /// Pass the arguments to the native transaction parser.
    Defer,
}

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
enum Origin {
    Valid,
    Invalid,
    Unsure,
}

#[derive(Default)]
struct Args<'a> {
    to: Option<&'a str>,
    checkout: Option<&'a str>,
    url: Option<&'a str>,
    json: bool,
    deployment_paths: Vec<&'a str>,
}

/// Python's `str.isspace`: Unicode `White_Space` plus the four information
/// separators U+001C..U+001F.
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Each option once, a value as the next argument. A value that starts with
/// `-`, a joined `--opt=value`, an abbreviation, a repeat, a positional or a
/// missing value returns `None`: the native parsers and `usage_problem`
/// answer those.
fn parse(arguments: &[String]) -> Option<Args<'_>> {
    let mut args = Args::default();
    let mut seen: Vec<&str> = Vec::new();
    let mut tokens = arguments.iter();
    while let Some(token) = tokens.next() {
        let token = token.as_str();
        if seen.contains(&token) {
            return None;
        }
        seen.push(token);
        if FLAGS.contains(&token) {
            args.json |= token == "--json";
            continue;
        }
        let value = tokens.next()?;
        if value.starts_with('-') {
            return None;
        }
        match token {
            "--to" => args.to = Some(value.as_str()),
            "--target-checkout" => args.checkout = Some(value.as_str()),
            "--target-url" => args.url = Some(value.as_str()),
            "--target-env-file" | "--target-compose-file" | "--target-work-dir" => {
                args.deployment_paths.push(value.as_str());
            }
            _ => return None,
        }
    }
    Some(args)
}

/// `codex_connection._validated_daemon_url`, answered only where every
/// supported Python agrees: `Valid` for a plain `http(s)://host[:port][/]`,
/// `Invalid` where some required part is certainly wrong, `Unsure` otherwise.
fn origin(url: &str) -> Origin {
    use Origin::{Invalid, Unsure, Valid};
    // Any whitespace or C0 character fails the oracle's last check.
    if url.chars().any(|c| py_isspace(c) || c < ' ') {
        return Invalid;
    }
    // urlsplit's scheme is the text before the first ':' (lowercased).
    let Some((scheme, rest)) = url.split_once(':') else {
        return Invalid;
    };
    if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
        return Invalid;
    }
    // No "//": no netloc, so no hostname.
    let Some(rest) = rest.strip_prefix("//") else {
        return Invalid;
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (netloc, suffix) = rest.split_at(end);
    let (unfragmented, fragment) = suffix.split_once('#').unwrap_or((suffix, ""));
    let (path, query) = unfragmented.split_once('?').unwrap_or((unfragmented, ""));
    if !matches!(path, "" | "/") || !query.is_empty() || !fragment.is_empty() {
        return Invalid;
    }
    let (userinfo, hostinfo) = netloc
        .rsplit_once('@')
        .map_or((None, netloc), |(user, host)| (Some(user), host));
    if let Some(userinfo) = userinfo {
        let (user, password) = userinfo.split_once(':').unwrap_or((userinfo, ""));
        if !user.is_empty() || !password.is_empty() {
            return Invalid;
        }
    }
    // Bracketed hosts are validated differently across Python releases.
    if hostinfo.contains(['[', ']']) {
        return Unsure;
    }
    let (host, port) = hostinfo
        .split_once(':')
        .map_or((hostinfo, None), |(host, port)| (host, Some(port)));
    if host.is_empty() {
        return Invalid;
    }
    if let Some(port) = port {
        // int() never reads an ASCII letter or other punctuation, and no
        // release reads a port above 65535; '+', '-', '_' and non-ASCII
        // digits differ between releases.
        if port
            .bytes()
            .any(|b| b.is_ascii() && !b.is_ascii_digit() && !b"+-_".contains(&b))
        {
            return Invalid;
        }
        let digits = port.trim_start_matches('0');
        if port.bytes().all(|b| b.is_ascii_digit())
            && (digits.len() > 5 || digits.parse::<u32>().is_ok_and(|value| value > 65535))
        {
            return Invalid;
        }
    }
    let plain_host = host
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-');
    let plain_port = port.is_none_or(|port| {
        (1..=5).contains(&port.len())
            && port.bytes().all(|b| b.is_ascii_digit())
            && port.parse::<u32>().is_ok_and(|value| value <= 65535)
    });
    if userinfo.is_none() && plain_host && plain_port {
        Valid
    } else {
        Unsure
    }
}

/// True when CPython decodes a POSIX argv as UTF-8: UTF-8 mode
/// (`PYTHONUTF8=1`, or unset under the C or POSIX locale, PEP 540) or a
/// UTF-8 locale codeset. The locale is the first non-empty of `LC_ALL`,
/// `LC_CTYPE`, `LANG`, as `setlocale` reads them. Anything else (another
/// codeset, `PYTHONUTF8=0` or a malformed value) is not answered.
fn posix_utf8_argv(var: impl Fn(&str) -> Option<String>) -> bool {
    let utf8_mode = var("PYTHONUTF8");
    if utf8_mode.as_deref() == Some("1") {
        return true;
    }
    if utf8_mode.is_some_and(|value| !value.is_empty()) {
        return false;
    }
    let locale = ["LC_ALL", "LC_CTYPE", "LANG"]
        .into_iter()
        .find_map(|name| var(name).filter(|value| !value.is_empty()));
    let Some(locale) = locale else {
        return true;
    };
    if matches!(locale.as_str(), "C" | "POSIX") {
        return true;
    }
    let codeset = locale
        .split_once('.')
        .map_or("", |(_, rest)| rest.split('@').next().unwrap_or(""));
    codeset.eq_ignore_ascii_case("utf-8") || codeset.eq_ignore_ascii_case("utf8")
}

/// Windows hands CPython the wide command line, so argv is exact there.
pub(super) fn utf8_argv() -> bool {
    cfg!(windows) || posix_utf8_argv(|name| std::env::var(name).ok())
}

/// The oracle's order: argparse (`_parser`, `main`), the `--target-url`
/// check, `main`'s `--to` check, then `Mover.run`'s `--to` and
/// `--target-checkout` checks.
pub(super) fn decide(arguments: &[String], context: &Context) -> Decision {
    // Outside UTF-8 decoding CPython sees other characters than these.
    if !context.utf8_argv && !arguments.iter().all(|argument| argument.is_ascii()) {
        return Decision::Defer;
    }
    let Some(args) = parse(arguments) else {
        return Decision::Defer;
    };
    let Some(to) = args.to else {
        return Decision::Refuse(missing_to());
    };
    match args.url.map(origin) {
        Some(Origin::Invalid) => return Decision::Refuse(BAD_URL.to_owned()),
        Some(Origin::Unsure) => return Decision::Defer,
        Some(Origin::Valid) | None => {}
    }
    // A value starting with '-' never gets here: parse() deferred it.
    if to.is_empty() || to.chars().any(py_isspace) {
        return Decision::Refuse(BAD_TO.to_owned());
    }
    // Under --json the native transaction parser answers this one, with
    // the same text.
    if to.chars().any(|c| c < ' ') {
        return if args.json {
            Decision::Defer
        } else {
            Decision::Refuse(BAD_TO.to_owned())
        };
    }
    // Any `--target-checkout` passes on: the native refusal says the
    // option is not supported, which is the cause whatever its value.
    if args
        .deployment_paths
        .iter()
        .any(|path| path.contains(['\n', '\r', '\0']))
    {
        return if args.json {
            Decision::Defer
        } else {
            Decision::Refuse(BAD_DEPLOY_PATH.to_owned())
        };
    }
    Decision::Defer
}

/// Writes `bytes`; on failure, how many bytes the stream took first.
pub(super) fn emit(out: &mut impl Write, bytes: &[u8]) -> Result<(), usize> {
    let mut written = 0;
    while written < bytes.len() {
        match out.write(&bytes[written..]) {
            Ok(0) => return Err(written),
            Ok(count) => written += count,
            Err(error) if error.kind() == ErrorKind::Interrupted => {}
            Err(_) => return Err(written),
        }
    }
    out.flush().map_err(|_| written)
}

/// A failed answer write ends the command, including an empty write.
fn settle(code: u8, written: Result<(), usize>) -> u8 {
    if written.is_ok() { code } else { 1 }
}

/// The usage block of the help and the missing `--to` line.
fn missing_to() -> String {
    let usage = &HELP[..HELP.find("\n\n").map_or(HELP.len(), |end| end + 1)];
    format!("{usage}{REQUIRED}")
}

/// The refusal (exit 2) for argv that neither native parser takes, naming
/// the first problem in argv order. Nothing has been read or changed.
pub(super) fn invalid_arguments(arguments: &[OsString]) -> u8 {
    settle(
        2,
        emit(
            &mut io::stderr().lock(),
            &super::text_bytes(&usage_refusal(arguments)),
        ),
    )
}

/// What `invalid_arguments` prints.
pub(super) fn usage_refusal(arguments: &[OsString]) -> String {
    match usage_problem(arguments) {
        Some(problem) => format!("move: {problem}. Nothing was changed\n"),
        None => missing_to(),
    }
}

/// The first problem in argv, or `None` when the only one is a missing
/// `--to`.
fn usage_problem(arguments: &[OsString]) -> Option<String> {
    let mut words = Vec::with_capacity(arguments.len());
    for (index, argument) in arguments.iter().enumerate() {
        let Some(word) = argument.to_str() else {
            return Some(format!(
                "argument {} is not valid Unicode text; retype it",
                index + 1
            ));
        };
        words.push(word);
    }
    if let Some(recovery) = words
        .iter()
        .map(|word| option_name(word))
        .find(|name| RECOVERY.contains(name))
    {
        // `--release-target` beside `--to` is most likely `--target-release`.
        if recovery == "--release-target" && words.iter().any(|word| option_name(word) == "--to") {
            return Some(
                "--release-target MOVE-ID is the recovery form that gives a rolled-back \
                 move's target its own bank back; for a move, the flag is --target-release"
                    .to_owned(),
            );
        }
        return Some(recovery_problem(&words, recovery));
    }
    forward_problem(&words)
}

/// `--name` for `--name=value`, otherwise the word itself.
fn option_name(word: &str) -> &str {
    match word.split_once('=') {
        Some((name, _)) if name.starts_with("--") => name,
        _ => word,
    }
}

/// A word shown back only when it is spelled like an option: one or two
/// dashes, then lowercase ASCII letters and dashes. Anything else (a value,
/// a path, a pasted secret, which carry digits or capitals) is named by its
/// position.
fn shown(word: &str, index: usize) -> String {
    let body = word
        .strip_prefix("--")
        .or_else(|| word.strip_prefix('-'))
        .unwrap_or("");
    let plain = word.len() <= 40
        && body.starts_with(|c: char| c.is_ascii_lowercase())
        && body.bytes().all(|b| b.is_ascii_lowercase() || b == b'-');
    if plain {
        word.to_owned()
    } else {
        format!("argument {}", index + 1)
    }
}

const NO_JOINED_VALUE: &str = "write the option and its value as two arguments";

fn forward_problem(words: &[&str]) -> Option<String> {
    let mut seen: Vec<&str> = Vec::new();
    let mut to = false;
    let mut index = 0;
    while index < words.len() {
        let word = words[index];
        let name = option_name(word);
        let takes_value = VALUE_OPTIONS.contains(&name);
        if !takes_value && !FLAGS.contains(&name) {
            return Some(unknown(word, index));
        }
        // Whatever its spelling or value, the option itself is the cause.
        if name == "--target-checkout" {
            return Some(
                "--target-checkout is not supported: native move needs an installed release \
                 deployment on the target; omit --target-checkout"
                    .to_owned(),
            );
        }
        if name != word {
            return Some(if takes_value {
                format!("{NO_JOINED_VALUE} ({name} VALUE); move does not read {name}=VALUE")
            } else {
                format!("{name} takes no value")
            });
        }
        // Compose files repeat, one per overlay, in order.
        if seen.contains(&name) && name != "--target-compose-file" {
            return Some(format!("{name} is given twice; give it once"));
        }
        seen.push(name);
        if takes_value {
            if words
                .get(index + 1)
                .is_none_or(|value| value.starts_with('-'))
            {
                return Some(if name == "--to" {
                    "--to needs the target host as ssh takes it, such as --to user@box; the value cannot start with -"
                        .to_owned()
                } else {
                    format!("{name} needs a value after it, and the value cannot start with -")
                });
            }
            to |= name == "--to";
            index += 1;
        }
        index += 1;
    }
    // Both parsers take every argv that passes this walk with a `--to`.
    to.then(|| {
        "these arguments do not form a move command; see pseudolife-mcp move --help".to_owned()
    })
}

/// Options that are neither forward options nor recovery forms, for the
/// abbreviation check.
const OTHER_OPTIONS: [&str; 2] = ["--target-gone", "--help"];

fn unknown(word: &str, index: usize) -> String {
    match option_name(word) {
        "--target-gone" => {
            return "--target-gone applies only to the recovery form pseudolife-mcp move --release-target MOVE-ID --target-gone"
                .to_owned();
        }
        "--help" => return "--help takes no value; run pseudolife-mcp move --help".to_owned(),
        _ => {}
    }
    if !word.starts_with('-') {
        return format!(
            "argument {} is neither an option nor an option's value; each value follows its option, such as --to user@box",
            index + 1
        );
    }
    let name = option_name(word);
    let full: Vec<&str> = VALUE_OPTIONS
        .iter()
        .chain(&FLAGS)
        .chain(&RECOVERY)
        .chain(&OTHER_OPTIONS)
        .copied()
        .filter(|option| name.len() > 2 && option.starts_with(name))
        .collect();
    match full.as_slice() {
        [one] => format!(
            "{} is short for {one}; write the full option name",
            shown(name, index)
        ),
        [] => format!(
            "{} is not a move option; see pseudolife-mcp move --help",
            shown(word, index)
        ),
        many => format!(
            "{} could be {}; write the full option name",
            shown(name, index),
            many.join(" or ")
        ),
    }
}

/// Problems with `move --rollback MOVE-ID [--yes] [--json]` and
/// `move --release-target MOVE-ID [--target-gone] [--yes] [--json]`.
fn recovery_problem(words: &[&str], recovery: &str) -> String {
    let release = recovery == "--release-target";
    let takes = if release {
        "a move id, --target-gone, --yes and --json"
    } else {
        "a move id, --yes and --json"
    };
    let mut chosen: Option<&str> = None;
    let mut seen: Vec<&str> = Vec::new();
    let mut index = 0;
    while index < words.len() {
        let word = words[index];
        let name = option_name(word);
        if RECOVERY.contains(&name) {
            if name != word {
                return format!("{NO_JOINED_VALUE} ({name} MOVE-ID)");
            }
            if let Some(first) = chosen {
                return if first == name {
                    format!("{name} is given twice; give it once")
                } else {
                    "give --rollback or --release-target, not both".to_owned()
                };
            }
            chosen = Some(name);
            if words.get(index + 1).is_none_or(|id| id.starts_with('-')) {
                return format!(
                    "{name} needs the id of the move to recover, as {name} MOVE-ID (the failed move printed it)"
                );
            }
            index += 1;
        } else if matches!(name, "--yes" | "--json" | "--target-gone") {
            if name != word {
                return format!("{name} takes no value");
            }
            if seen.contains(&name) {
                return format!("{name} is given twice; give it once");
            }
            seen.push(name);
        } else {
            let what = if word.starts_with('-') {
                shown(name, index)
            } else {
                format!("argument {}", index + 1)
            };
            return format!("{what} cannot be used with {recovery}, which takes only {takes}");
        }
        index += 1;
    }
    if seen.contains(&"--target-gone") && !release {
        return "--target-gone applies only to --release-target MOVE-ID, not to --rollback"
            .to_owned();
    }
    format!("{recovery} takes only {takes}; see pseudolife-mcp move --help")
}

/// Help wins wherever it appears, as argparse's does, even beside argv that
/// is not valid Unicode.
pub(super) fn wants_help(arguments: &[OsString]) -> bool {
    arguments
        .iter()
        .any(|argument| argument == "--help" || argument == "-h")
}

/// A missing answer passes the arguments to the native transaction parser.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    if wants_help(&arguments) {
        return Some(settle(
            0,
            emit(&mut io::stdout().lock(), &super::text_bytes(HELP)),
        ));
    }
    let arguments = arguments
        .into_iter()
        .map(OsString::into_string)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let context = Context {
        utf8_argv: utf8_argv(),
    };
    match decide(&arguments, &context) {
        Decision::Defer => None,
        Decision::Refuse(text) => Some(settle(
            2,
            emit(&mut io::stderr().lock(), &super::text_bytes(&text)),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    fn plain() -> Context {
        Context { utf8_argv: true }
    }

    #[test]
    fn isspace_matches_python_over_every_code_point() {
        // [c for c in range(0x110000) if chr(c).isspace()] on CPython 3.11.
        let python: Vec<u32> = [
            0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
        ]
        .into_iter()
        .chain(0x2000..=0x200a)
        .chain([0x2028, 0x2029, 0x202f, 0x205f, 0x3000])
        .collect();
        let rust: Vec<u32> = (0..=0x10ffff_u32)
            .filter_map(char::from_u32)
            .filter(|c| py_isspace(*c))
            .map(u32::from)
            .collect();
        assert_eq!(rust, python);
    }

    #[test]
    fn origins_answer_only_where_python_agrees() {
        use Origin::{Invalid, Unsure, Valid};
        // Valid/Invalid rows were checked against _validated_daemon_url.
        for (url, want) in [
            ("http://100.64.0.2:8765", Valid),
            ("https://box.example.com", Valid),
            ("HTTPS://Box.example.com:443/", Valid),
            ("http://h:0", Valid),
            ("http://h:65535", Valid),
            ("http://100.64.0.2:8765/api", Invalid),
            ("http://h/?q=1", Invalid),
            ("http://h#frag", Invalid),
            ("ftp://h", Invalid),
            ("100.64.0.2:8765", Invalid),
            ("localhost:8765", Invalid),
            ("http:h", Invalid),
            ("http:/h", Invalid),
            ("", Invalid),
            ("http://", Invalid),
            ("http://:8765", Invalid),
            ("http://u@h", Invalid),
            ("http://:p@h", Invalid),
            ("http://h:abc", Invalid),
            ("http://h:1:2", Invalid),
            ("http://h:99999", Invalid),
            ("http://h:0000099999", Invalid),
            ("http://h :1", Invalid),
            ("http://h\u{1}", Invalid),
            ("http://h\u{a0}", Invalid),
            ("http://[::1]:8765", Unsure),
            ("http://[::1", Unsure),
            ("http://@h", Unsure),
            ("http://h:", Unsure),
            ("http://h:+80", Unsure),
            ("http://h:065535", Unsure),
            ("http://h/?", Valid),
            ("http://h#", Valid),
            ("http://h_x", Unsure),
            ("http://\u{e4}.example", Unsure),
        ] {
            assert_eq!(origin(url), want, "{url:?}");
        }
    }

    #[test]
    fn every_canonical_valid_argv_defers() {
        for items in [
            &["--to", "root@box"][..],
            &["--to", "root@box", "--dry-run"],
            &["--to", "box", "--resume", "--yes"],
            &["--to", "box", "--json"],
            &[
                "--to",
                "root@box",
                "--target-url",
                "http://100.64.0.2:8765",
                "--target-checkout",
                "/srv/pl",
                "--no-keep-tokens",
                "--resume",
                "--dry-run",
                "--yes",
                "--json",
            ],
            &["--to", "a\u{7f}b"],
            &["--to", "box", "--target-checkout", "a\tb"],
            &["--to", "box", "--target-checkout", ""],
        ] {
            assert_eq!(decide(&argv(items), &plain()), Decision::Defer, "{items:?}");
        }
    }

    #[test]
    fn argparse_shapes_pass_to_the_native_parsers() {
        for items in [
            &["--to=box"][..],
            &["--to", "-1"],
            &["--to", "-"],
            &["--to"],
            &["--dry", "--to", "box"],
            &["--yes", "--yes", "--to", "box"],
            &["--to", "a", "--to", "b"],
            &["box"],
            &["--to", "box", "extra"],
            &["--rollback", "20261121T000000Z-ab12"],
        ] {
            assert_eq!(decide(&argv(items), &plain()), Decision::Defer, "{items:?}");
        }
    }

    #[test]
    fn help_answers_wherever_it_appears() {
        for items in [
            &["--help"][..],
            &["-h"],
            &["--help", "--to", "box"],
            &["--to", "box", "--dry-run", "-h"],
            &["--to=box", "--help"],
            &["--rollback", "--help"],
        ] {
            let arguments: Vec<OsString> = items.iter().map(OsString::from).collect();
            assert!(wants_help(&arguments), "{items:?}");
        }
        let plain_argv: Vec<OsString> = ["--to", "box", "--hel"]
            .iter()
            .map(OsString::from)
            .collect();
        assert!(!wants_help(&plain_argv));
    }

    #[test]
    fn refusals_follow_the_oracle_order() {
        let refuse = |text: &str| Decision::Refuse(text.to_owned());
        let bad_url = &["--to", "a b", "--target-url", "http://h/x"][..];
        assert_eq!(decide(&argv(bad_url), &plain()), refuse(BAD_URL));
        let unsure = &["--to", "a b", "--target-url", "http://[::1]"][..];
        assert_eq!(decide(&argv(unsure), &plain()), Decision::Defer);
        let fine = &["--to", "a b", "--target-url", "http://h:1"][..];
        assert_eq!(decide(&argv(fine), &plain()), refuse(BAD_TO));
        assert_eq!(decide(&argv(&["--to", ""]), &plain()), refuse(BAD_TO));
        assert_eq!(
            decide(&argv(&["--to", "a\u{1c}b", "--json"]), &plain()),
            refuse(BAD_TO)
        );
        assert_eq!(
            decide(&argv(&["--to", "a\u{1}b"]), &plain()),
            refuse(BAD_TO)
        );
        assert_eq!(
            decide(&argv(&["--json", "--to", "a\u{1}b"]), &plain()),
            Decision::Defer
        );
        let checkout = &["--to", "a\u{1}b", "--target-checkout", "x\ny"][..];
        assert_eq!(decide(&argv(checkout), &plain()), refuse(BAD_TO));
        // Any checkout passes on to the native "not supported" refusal.
        for path in ["x\ny", "x\r", "\r/", "/srv/pl"] {
            let items = &["--to", "box", "--target-checkout", path][..];
            assert_eq!(decide(&argv(items), &plain()), Decision::Defer);
        }
        let json = &["--to", "box", "--target-checkout", "x\n", "--json"][..];
        assert_eq!(decide(&argv(json), &plain()), Decision::Defer);
    }

    #[test]
    fn missing_to_is_the_usage_error_whatever_the_terminal() {
        // The width, a terminal and colour variables no longer change it.
        for items in [&[][..], &["--yes"], &["--dry-run", "--json"]] {
            let Decision::Refuse(text) = decide(&argv(items), &plain()) else {
                panic!("expected the usage error for {items:?}");
            };
            assert!(text.starts_with("usage: pseudolife-mcp move [-h] --to SSH-TARGET"));
            assert!(text.ends_with(&format!("[--json]\n{REQUIRED}")));
            assert_eq!(text.matches('\n').count(), 8);
            assert_eq!(text, missing_to());
        }
    }

    fn refusal_for(items: &[&str]) -> String {
        let arguments: Vec<OsString> = items.iter().map(OsString::from).collect();
        usage_refusal(&arguments)
    }

    #[test]
    fn every_declined_shape_names_its_cause() {
        let nothing = ". Nothing was changed\n";
        for (items, problem) in [
            (
                &["--to=root@box"][..],
                "write the option and its value as two arguments (--to VALUE); move does not read --to=VALUE",
            ),
            (&["--to", "box", "--yes=1"], "--yes takes no value"),
            (
                &["--dry", "--to", "box"],
                "--dry is short for --dry-run; write the full option name",
            ),
            (
                &["--target", "x", "--to", "box"],
                "--target could be --target-checkout or --target-url or --target-env-file or --target-compose-file or --target-work-dir or --target-release or --target-gone; write the full option name",
            ),
            (
                &["--he"],
                "--he is short for --help; write the full option name",
            ),
            (
                &["--help=x"],
                "--help takes no value; run pseudolife-mcp move --help",
            ),
            (
                &["--to", "box", "--target-gone=1"],
                "--target-gone applies only to the recovery form pseudolife-mcp move --release-target MOVE-ID --target-gone",
            ),
            (
                &["--to", "box", "--yes", "-AbC123def456"],
                "argument 4 is not a move option; see pseudolife-mcp move --help",
            ),
            (
                &["--to", "box", "--release-target"],
                "--release-target MOVE-ID is the recovery form that gives a rolled-back move's target its own bank back; for a move, the flag is --target-release",
            ),
            (
                &["--to", "box", "--bogus"],
                "--bogus is not a move option; see pseudolife-mcp move --help",
            ),
            (
                &["--to", "box", "--token=s3cret"],
                "argument 3 is not a move option; see pseudolife-mcp move --help",
            ),
            (
                &["--yes", "--yes", "--to", "box"],
                "--yes is given twice; give it once",
            ),
            (
                &["--to", "a", "--to", "b"],
                "--to is given twice; give it once",
            ),
            (
                &["--to", "box", "extra"],
                "argument 3 is neither an option nor an option's value; each value follows its option, such as --to user@box",
            ),
            (
                &["--to"],
                "--to needs the target host as ssh takes it, such as --to user@box; the value cannot start with -",
            ),
            (
                &["--to", "-oProxy=x"],
                "--to needs the target host as ssh takes it, such as --to user@box; the value cannot start with -",
            ),
            (
                &["--to", "box", "--target-checkout", "-x"],
                "--target-checkout is not supported: native move needs an installed release deployment on the target; omit --target-checkout",
            ),
            (
                &["--target-checkout=/srv", "--to", "box"],
                "--target-checkout is not supported: native move needs an installed release deployment on the target; omit --target-checkout",
            ),
            (
                &["--to", "box", "--target-url", "-x"],
                "--target-url needs a value after it, and the value cannot start with -",
            ),
            (
                &["--to", "box", "--target-gone"],
                "--target-gone applies only to the recovery form pseudolife-mcp move --release-target MOVE-ID --target-gone",
            ),
            (
                &["--rollback"],
                "--rollback needs the id of the move to recover, as --rollback MOVE-ID (the failed move printed it)",
            ),
            (
                &["--rollback", "--yes"],
                "--rollback needs the id of the move to recover, as --rollback MOVE-ID (the failed move printed it)",
            ),
            (
                &["--rollback=abc"],
                "write the option and its value as two arguments (--rollback MOVE-ID)",
            ),
            (
                &["--rollback", "a", "--rollback", "b"],
                "--rollback is given twice; give it once",
            ),
            (
                &["--rollback", "a", "--release-target", "b"],
                "give --rollback or --release-target, not both",
            ),
            (
                &["--rollback", "a", "--target-gone"],
                "--target-gone applies only to --release-target MOVE-ID, not to --rollback",
            ),
            (
                &["--rollback", "a", "--to", "box"],
                "--to cannot be used with --rollback, which takes only a move id, --yes and --json",
            ),
            (
                &["--release-target", "a", "extra"],
                "argument 3 cannot be used with --release-target, which takes only a move id, --target-gone, --yes and --json",
            ),
            (
                &["--release-target", "a", "--yes", "--yes"],
                "--yes is given twice; give it once",
            ),
        ] {
            assert_eq!(
                refusal_for(items),
                format!("move: {problem}{nothing}"),
                "{items:?}"
            );
        }
        // Repeated compose files are overlays, not a repeat: with a --to
        // missing, that is the one problem named.
        assert_eq!(
            refusal_for(&["--target-compose-file", "/a", "--target-compose-file", "/b"]),
            missing_to()
        );
        assert_eq!(refusal_for(&[]), missing_to());
    }

    /// Takes `room` bytes, then fails.
    struct Full {
        room: usize,
    }

    impl Write for Full {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.room == 0 {
                return Err(io::Error::other("no space left on device"));
            }
            let count = bytes.len().min(self.room);
            self.room -= count;
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn a_failed_answer_write_ends_the_command() {
        let text = BAD_TO.as_bytes();
        assert_eq!(settle(2, emit(&mut Full { room: 0 }, text)), 1);
        assert_eq!(settle(2, emit(&mut Full { room: 5 }, text)), 1);
        let mut sink = Vec::new();
        assert_eq!(settle(2, emit(&mut sink, text)), 2);
        assert_eq!(sink, text);
        assert_eq!(settle(0, emit(&mut Full { room: 0 }, HELP.as_bytes())), 1);
    }

    #[test]
    fn non_ascii_argv_defers_unless_cpython_decodes_it_as_utf8() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |name: &str| {
                pairs
                    .iter()
                    .find(|(key, _)| *key == name)
                    .map(|(_, value)| (*value).to_owned())
            }
        };
        for (pairs, utf8) in [
            (&[][..], true),
            (&[("LANG", "C")], true),
            (&[("LANG", "POSIX")], true),
            (&[("LANG", "C.UTF-8")], true),
            (&[("LANG", "en_US.utf8")], true),
            (&[("LANG", "de_DE.UTF-8@euro")], true),
            (&[("LANG", "en_US.ISO-8859-1")], false),
            (&[("LANG", "en_US")], false),
            (
                &[("LC_ALL", "en_US.ISO-8859-1"), ("LANG", "C.UTF-8")],
                false,
            ),
            (
                &[
                    ("LC_ALL", ""),
                    ("LC_CTYPE", "C.UTF-8"),
                    ("LANG", "en_US.ISO-8859-1"),
                ],
                true,
            ),
            (
                &[("LC_CTYPE", "en_US.ISO-8859-1"), ("LANG", "C.UTF-8")],
                false,
            ),
            (&[("PYTHONUTF8", "1"), ("LANG", "en_US.ISO-8859-1")], true),
            (&[("PYTHONUTF8", "0"), ("LANG", "C.UTF-8")], false),
            (&[("PYTHONUTF8", "yes"), ("LANG", "C.UTF-8")], false),
        ] {
            assert_eq!(posix_utf8_argv(env(pairs)), utf8, "{pairs:?}");
        }
        let latin1 = Context { utf8_argv: false };
        for items in [
            &["--to", "root@\u{3000}box"][..],
            &["--to", "box", "--target-url", "http://box\u{a0}"],
            &["--to", "a b", "--target-checkout", "/srv/\u{e4}"],
        ] {
            assert_eq!(decide(&argv(items), &latin1), Decision::Defer, "{items:?}");
            assert_ne!(decide(&argv(items), &plain()), Decision::Defer);
        }
        let ascii = &["--to", "a b"][..];
        assert_eq!(
            decide(&argv(ascii), &latin1),
            Decision::Refuse(BAD_TO.to_owned())
        );
    }

    #[test]
    fn exposure_consent_preserves_pure_refusals_and_effectful_deferral() {
        assert_eq!(
            decide(&argv(&["--to", "box", "--expose-target"]), &plain()),
            Decision::Defer
        );
        assert_eq!(
            decide(
                &argv(&[
                    "--to",
                    "box",
                    "--expose-target",
                    "--target-url",
                    "ftp://box"
                ]),
                &plain()
            ),
            Decision::Refuse(BAD_URL.to_owned())
        );
    }

    #[test]
    fn clone_free_options_preserve_path_refusals_without_effects() {
        for option in [
            "--target-env-file",
            "--target-compose-file",
            "--target-work-dir",
        ] {
            assert_eq!(
                decide(&argv(&["--to", "box", option, "bad\npath"]), &plain()),
                Decision::Refuse(BAD_DEPLOY_PATH.to_owned())
            );
            assert_eq!(
                decide(
                    &argv(&["--to", "box", option, "bad\npath", "--json"]),
                    &plain()
                ),
                Decision::Defer
            );
        }
        assert_eq!(
            decide(
                &argv(&[
                    "--to",
                    "box",
                    "--target-release",
                    "--target-work-dir",
                    "/srv/moves"
                ]),
                &plain()
            ),
            Decision::Defer
        );
    }
}
