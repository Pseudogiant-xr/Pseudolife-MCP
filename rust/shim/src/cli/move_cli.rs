//! `move`: the refusals `pseudolife-mcp move` makes before any effect.
//!
//! Oracle: `pseudolife_memory/move_cli.py` (`_parser`, `main` and the first
//! two checks of `Mover.run`). Every argv that passes those checks goes on to
//! read the source daemon's `/health`, run docker and ssh, and start a move,
//! so it defers here. The decision is made from argv and `COLUMNS` alone,
//! before this process opens a file, a socket or a child process.
use std::{
    ffi::OsString,
    io::{self, Write},
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

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Decision {
    /// argparse's help on stdout, exit 0.
    Help,
    /// One refusal on stderr, exit 2 (`EXIT_USAGE`).
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

/// The oracle's order: argparse (`_parser`, `main`), the `--target-url`
/// check, `main`'s `--to` check, then `Mover.run`'s `--to` and
/// `--target-checkout` checks. `columns` is the `COLUMNS` variable, which
/// sets argparse's wrap width; only 80 is reproduced.
pub(super) fn decide(arguments: &[String], columns: Option<&str>) -> Decision {
    let wrapped_at_80 = columns == Some("80");
    if matches!(arguments, [only] if only == "--help" || only == "-h") {
        return if wrapped_at_80 {
            Decision::Help
        } else {
            Decision::Defer
        };
    }
    let Some(args) = parse(arguments) else {
        return Decision::Defer;
    };
    let Some(to) = args.to else {
        return if wrapped_at_80 {
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

/// `None` defers to the dispatcher before any effect.
pub(super) fn run(arguments: Vec<OsString>) -> Option<u8> {
    let arguments = arguments
        .into_iter()
        .map(OsString::into_string)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let columns = std::env::var("COLUMNS").ok();
    match decide(&arguments, columns.as_deref()) {
        Decision::Defer => None,
        Decision::Help => {
            let mut out = io::stdout().lock();
            out.write_all(&super::text_bytes(HELP))
                .and_then(|()| out.flush())
                .ok()
                .map(|()| 0)
        }
        Decision::Refuse(text) => {
            let _ = io::stderr().lock().write_all(&super::text_bytes(&text));
            Some(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
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
                decide(&argv(items), Some("80")),
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
                decide(&argv(items), Some("80")),
                Decision::Defer,
                "{items:?}"
            );
        }
        assert_eq!(decide(&argv(&["--help"]), Some("100")), Decision::Defer);
        assert_eq!(decide(&argv(&[]), None), Decision::Defer);
    }

    #[test]
    fn refusals_follow_the_oracle_order() {
        let refuse = |text: &str| Decision::Refuse(text.to_owned());
        let bad_url = &["--to", "a b", "--target-url", "http://h/x"][..];
        assert_eq!(decide(&argv(bad_url), None), refuse(BAD_URL));
        let unsure = &["--to", "a b", "--target-url", "http://[::1]"][..];
        assert_eq!(decide(&argv(unsure), None), Decision::Defer);
        let fine = &["--to", "a b", "--target-url", "http://h:1"][..];
        assert_eq!(decide(&argv(fine), None), refuse(BAD_TO));
        assert_eq!(decide(&argv(&["--to", ""]), None), refuse(BAD_TO));
        assert_eq!(
            decide(&argv(&["--to", "a\u{1c}b", "--json"]), None),
            refuse(BAD_TO)
        );
        assert_eq!(decide(&argv(&["--to", "a\u{1}b"]), None), refuse(BAD_TO));
        assert_eq!(
            decide(&argv(&["--json", "--to", "a\u{1}b"]), None),
            Decision::Defer
        );
        let checkout = &["--to", "a\u{1}b", "--target-checkout", "x\ny"][..];
        assert_eq!(decide(&argv(checkout), None), refuse(BAD_TO));
        for path in ["x\ny", "x\r", "\r/"] {
            let items = &["--to", "box", "--target-checkout", path][..];
            assert_eq!(decide(&argv(items), None), refuse(BAD_CHECKOUT));
        }
        let json = &["--to", "box", "--target-checkout", "x\n", "--json"][..];
        assert_eq!(decide(&argv(json), None), Decision::Defer);
    }

    #[test]
    fn missing_to_is_argparse_usage_at_80_columns() {
        let Decision::Refuse(text) = decide(&argv(&["--yes"]), Some("80")) else {
            panic!("expected the usage error");
        };
        assert!(text.starts_with("usage: pseudolife-mcp move [-h] --to SSH-TARGET"));
        assert!(text.ends_with(&format!("[--dry-run] [--yes] [--json]\n{REQUIRED}")));
        assert_eq!(text.matches('\n').count(), 4);
        assert_eq!(decide(&argv(&["--yes"]), Some("81")), Decision::Defer);
    }
}
