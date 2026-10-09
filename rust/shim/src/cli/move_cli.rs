//! `move`: the refusals `pseudolife-mcp move` makes before any effect.
//!
//! Oracle: `pseudolife_memory/move_cli.py` (`_parser`, `main` and the first
//! two checks of `Mover.run`). Every argv that passes those checks goes on to
//! read the source daemon's `/health`, run docker and ssh, and start a move,
//! so it defers here. The decision is made from argv and the environment
//! alone, and this module does no I/O beyond writing its one answer to
//! stdout or stderr (`tests/cli_move_contract.rs` scans it for that).
use std::{
    ffi::OsString,
    io::{self, ErrorKind, IsTerminal, Write},
};

/// `_parser().format_help()` at `COLUMNS=80` (argparse wraps at 78).
const HELP: &str = concat!(
    "usage: pseudolife-mcp move [-h] --to SSH-TARGET [--target-checkout PATH]\n",
    "                           [--target-url URL] [--no-keep-tokens] [--resume]\n",
    "                           [--dry-run] [--yes] [--json]\n",
    "\n",
    "Move this host's Docker-tier bank to another Docker-tier checkout host over\n",
    "key-based ssh: preflight, final backup from the stopped source, database\n",
    "fence, restore and verify on the target, start it once, re-point this\n",
    "machine's clients. A failure rolls back. Exit codes: 0 moved, 1 failed and\n",
    "rolled back, 2 usage or not confirmed, 4 refused before any change, 5 moved\n",
    "but re-pointing failed, 6 failed and the rollback could not finish. Run it\n",
    "under tmux, screen or nohup.\n",
    "\n",
    "options:\n",
    "  -h, --help            show this help message and exit\n",
    "  --to SSH-TARGET       the target host as ssh takes it (root@box, or a\n",
    "                        ~/.ssh/config alias)\n",
    "  --target-checkout PATH\n",
    "                        the checkout on the target (default ~/src/Pseudolife-\n",
    "                        MCP)\n",
    "  --target-url URL      the URL clients will use for the target (default: its\n",
    "                        `expose status`)\n",
    "  --no-keep-tokens      do not carry the environment identities; invite each\n",
    "                        machine on the target again\n",
    "  --resume              overwrite the half-restored bank an earlier attempt of\n",
    "                        this move left on the target\n",
    "  --dry-run             show the plan; change nothing\n",
    "  --yes                 move without asking\n",
    "  --json                one JSON report on stdout\n",
);
const REQUIRED: &str = "pseudolife-mcp move: error: the following arguments are required: --to\n";
const BAD_TO: &str = "move: --to takes an ssh target such as root@box or a ~/.ssh/config alias\n";
const BAD_URL: &str = "move: --target-url must be an http(s) origin such as http://100.64.0.2:8765 \
                       (no path, query or credentials)\n";
const BAD_CHECKOUT: &str = "move: --target-checkout holds a control character\n";
const FLAGS: [&str; 5] = [
    "--no-keep-tokens",
    "--resume",
    "--dry-run",
    "--yes",
    "--json",
];

/// What the oracle's output depends on besides argv.
pub(super) struct Context<'a> {
    /// `COLUMNS`, which sets argparse's wrap width; only 80 is reproduced.
    pub(super) columns: Option<&'a str>,
    /// CPython decodes this argv as UTF-8 (see `utf8_argv`).
    pub(super) utf8_argv: bool,
    /// argparse could colour what it writes to stdout / stderr.
    pub(super) colour_stdout: bool,
    pub(super) colour_stderr: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Decision {
    /// argparse's help on stdout, exit 0.
    Help,
    /// One refusal on stderr, exit 2 (`EXIT_USAGE`): argparse's usage error
    /// or one of `main` / `Mover.run`'s messages.
    Refuse(String),
    /// Everything else: the dispatcher's deferral line, exit 1.
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
}

/// Python's `str.isspace`: Unicode `White_Space` plus the four information
/// separators U+001C..U+001F.
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Each option once, a value as the next argument. A value that starts with
/// `-`, a joined `--opt=value`, an abbreviation, a repeat, a positional or a
/// missing value is argparse's business and defers.
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
        let slot = match token {
            "--to" => &mut args.to,
            "--target-checkout" => &mut args.checkout,
            "--target-url" => &mut args.url,
            _ => return None,
        };
        let value = tokens.next()?;
        if value.starts_with('-') {
            return None;
        }
        *slot = Some(value.as_str());
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
fn utf8_argv() -> bool {
    cfg!(windows) || posix_utf8_argv(|name| std::env::var(name).ok())
}

/// argparse colours its help and usage on a terminal, or as `FORCE_COLOR`,
/// `PYTHON_COLORS` and `NO_COLOR` say (newer CPython); any of them set defers.
fn colour_env(var: impl Fn(&str) -> bool) -> bool {
    ["FORCE_COLOR", "PYTHON_COLORS", "NO_COLOR"]
        .into_iter()
        .any(var)
}

/// The oracle's order: argparse (`_parser`, `main`), the `--target-url`
/// check, `main`'s `--to` check, then `Mover.run`'s `--to` and
/// `--target-checkout` checks.
pub(super) fn decide(arguments: &[String], context: &Context<'_>) -> Decision {
    let wrapped_at_80 = context.columns == Some("80");
    // Outside UTF-8 decoding CPython sees other characters than these.
    if !context.utf8_argv && !arguments.iter().all(|argument| argument.is_ascii()) {
        return Decision::Defer;
    }
    if matches!(arguments, [only] if only == "--help" || only == "-h") {
        return if wrapped_at_80 && !context.colour_stdout {
            Decision::Help
        } else {
            Decision::Defer
        };
    }
    let Some(args) = parse(arguments) else {
        return Decision::Defer;
    };
    let Some(to) = args.to else {
        return if wrapped_at_80 && !context.colour_stderr {
            let usage = &HELP[..HELP.find("\n\n").map_or(HELP.len(), |end| end + 1)];
            Decision::Refuse(format!("{usage}{REQUIRED}"))
        } else {
            Decision::Defer
        };
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
    // Mover.run's refusals go through its report: under --json that is a
    // JSON document carrying a fresh move id on stdout, which defers.
    if to.chars().any(|c| c < ' ') {
        return if args.json {
            Decision::Defer
        } else {
            Decision::Refuse(BAD_TO.to_owned())
        };
    }
    // `checkout.rstrip("/") or DEFAULT` keeps any CR or LF; NUL cannot
    // arrive in argv.
    if args
        .checkout
        .is_some_and(|path| path.contains(['\n', '\r']))
    {
        return if args.json {
            Decision::Defer
        } else {
            Decision::Refuse(BAD_CHECKOUT.to_owned())
        };
    }
    Decision::Defer
}

/// Writes `bytes`; on failure, how many bytes the stream took first.
fn emit(out: &mut impl Write, bytes: &[u8]) -> Result<(), usize> {
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

/// A write that failed before any byte left defers: nothing was answered
/// yet. One that failed part-way exits 1, where CPython raises from
/// `print` (its traceback write fails too; the exit is 1 or 120).
fn settle(code: u8, written: Result<(), usize>) -> Option<u8> {
    match written {
        Ok(()) => Some(code),
        Err(0) => None,
        Err(_) => Some(1),
    }
}

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    let arguments = arguments
        .into_iter()
        .map(OsString::into_string)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let columns = std::env::var("COLUMNS").ok();
    let colour = colour_env(|name| std::env::var_os(name).is_some());
    let context = Context {
        columns: columns.as_deref(),
        utf8_argv: utf8_argv(),
        colour_stdout: colour || io::stdout().is_terminal(),
        colour_stderr: colour || io::stderr().is_terminal(),
    };
    match decide(&arguments, &context) {
        Decision::Defer => None,
        Decision::Help => settle(0, emit(&mut io::stdout().lock(), &super::text_bytes(HELP))),
        Decision::Refuse(text) => {
            settle(2, emit(&mut io::stderr().lock(), &super::text_bytes(&text)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    fn plain(columns: Option<&str>) -> Context<'_> {
        Context {
            columns,
            utf8_argv: true,
            colour_stdout: false,
            colour_stderr: false,
        }
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
            assert_eq!(
                decide(&argv(items), &plain(Some("80"))),
                Decision::Defer,
                "{items:?}"
            );
        }
    }

    #[test]
    fn argparse_shapes_defer() {
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
            &["--help", "--to", "box"],
        ] {
            assert_eq!(
                decide(&argv(items), &plain(Some("80"))),
                Decision::Defer,
                "{items:?}"
            );
        }
        assert_eq!(
            decide(&argv(&["--help"]), &plain(Some("100"))),
            Decision::Defer
        );
        assert_eq!(decide(&argv(&[]), &plain(None)), Decision::Defer);
    }

    #[test]
    fn refusals_follow_the_oracle_order() {
        let refuse = |text: &str| Decision::Refuse(text.to_owned());
        let bad_url = &["--to", "a b", "--target-url", "http://h/x"][..];
        assert_eq!(decide(&argv(bad_url), &plain(None)), refuse(BAD_URL));
        let unsure = &["--to", "a b", "--target-url", "http://[::1]"][..];
        assert_eq!(decide(&argv(unsure), &plain(None)), Decision::Defer);
        let fine = &["--to", "a b", "--target-url", "http://h:1"][..];
        assert_eq!(decide(&argv(fine), &plain(None)), refuse(BAD_TO));
        assert_eq!(decide(&argv(&["--to", ""]), &plain(None)), refuse(BAD_TO));
        assert_eq!(
            decide(&argv(&["--to", "a\u{1c}b", "--json"]), &plain(None)),
            refuse(BAD_TO)
        );
        assert_eq!(
            decide(&argv(&["--to", "a\u{1}b"]), &plain(None)),
            refuse(BAD_TO)
        );
        assert_eq!(
            decide(&argv(&["--json", "--to", "a\u{1}b"]), &plain(None)),
            Decision::Defer
        );
        let checkout = &["--to", "a\u{1}b", "--target-checkout", "x\ny"][..];
        assert_eq!(decide(&argv(checkout), &plain(None)), refuse(BAD_TO));
        for path in ["x\ny", "x\r", "\r/"] {
            let items = &["--to", "box", "--target-checkout", path][..];
            assert_eq!(decide(&argv(items), &plain(None)), refuse(BAD_CHECKOUT));
        }
        let json = &["--to", "box", "--target-checkout", "x\n", "--json"][..];
        assert_eq!(decide(&argv(json), &plain(None)), Decision::Defer);
    }

    #[test]
    fn missing_to_is_argparse_usage_at_80_columns() {
        let Decision::Refuse(text) = decide(&argv(&["--yes"]), &plain(Some("80"))) else {
            panic!("expected the usage error");
        };
        assert!(text.starts_with("usage: pseudolife-mcp move [-h] --to SSH-TARGET"));
        assert!(text.ends_with(&format!("[--dry-run] [--yes] [--json]\n{REQUIRED}")));
        assert_eq!(text.matches('\n').count(), 4);
        assert_eq!(
            decide(&argv(&["--yes"]), &plain(Some("81"))),
            Decision::Defer
        );
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
    fn a_failed_answer_write_defers_only_when_nothing_left() {
        let text = BAD_TO.as_bytes();
        // stderr on /dev/full: nothing was written, so nothing was answered.
        assert_eq!(settle(2, emit(&mut Full { room: 0 }, text)), None);
        // part of the answer left: CPython's print raised, exit 1 (or 120).
        assert_eq!(settle(2, emit(&mut Full { room: 5 }, text)), Some(1));
        let mut sink = Vec::new();
        assert_eq!(settle(2, emit(&mut sink, text)), Some(2));
        assert_eq!(sink, text);
        assert_eq!(
            settle(0, emit(&mut Full { room: 0 }, HELP.as_bytes())),
            None
        );
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
        let latin1 = Context {
            utf8_argv: false,
            ..plain(Some("80"))
        };
        for items in [
            &["--to", "root@\u{3000}box"][..],
            &["--to", "box", "--target-url", "http://box\u{a0}"],
            &["--to", "a b", "--target-checkout", "/srv/\u{e4}"],
        ] {
            assert_eq!(decide(&argv(items), &latin1), Decision::Defer, "{items:?}");
            assert_ne!(decide(&argv(items), &plain(Some("80"))), Decision::Defer);
        }
        let ascii = &["--to", "a b"][..];
        assert_eq!(
            decide(&argv(ascii), &latin1),
            Decision::Refuse(BAD_TO.to_owned())
        );
    }

    #[test]
    fn argparse_output_defers_when_it_could_be_coloured() {
        let tty_out = Context {
            colour_stdout: true,
            ..plain(Some("80"))
        };
        let tty_err = Context {
            colour_stderr: true,
            ..plain(Some("80"))
        };
        assert_eq!(decide(&argv(&["--help"]), &tty_out), Decision::Defer);
        assert_eq!(decide(&argv(&["-h"]), &tty_err), Decision::Help);
        assert_eq!(decide(&argv(&["--yes"]), &tty_err), Decision::Defer);
        assert_ne!(decide(&argv(&["--yes"]), &tty_out), Decision::Defer);
        // main's own messages are plain print() calls: never coloured.
        let refusal = &["--to", ""][..];
        assert_eq!(
            decide(&argv(refusal), &tty_err),
            Decision::Refuse(BAD_TO.to_owned())
        );
        for name in ["FORCE_COLOR", "PYTHON_COLORS", "NO_COLOR"] {
            assert!(colour_env(|var| var == name), "{name}");
        }
        assert!(!colour_env(|var| var == "TERM"));
    }
}
