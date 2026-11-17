//! Native argument parsing for expose; errors precede host access.

/// `pseudolife-mcp expose -h` at `COLUMNS=80` (argparse wraps at 78).
pub(super) const MAIN_HELP: &str = concat!(
    "usage: pseudolife-mcp expose [-h] {tailscale,off,status} ...\n",
    "\n",
    "Make this host's daemon reachable from the tailnet (Tailscale Serve, TCP).\n",
    "\n",
    "positional arguments:\n",
    "  {tailscale,off,status}\n",
    "    tailscale           forward tailnet port <port> to the daemon on\n",
    "                        127.0.0.1:<port>\n",
    "    off                 remove that forward (never anything else on the port)\n",
    "    status              print the client URL, or why there is none\n",
    "\n",
    "options:\n",
    "  -h, --help            show this help message and exit\n",
);

const OPTIONS: &str = concat!(
    "options:\n",
    "  -h, --help   show this help message and exit\n",
    "  --port PORT  the daemon's port, and the tailnet port (default 8765)\n",
);

/// The three actions, in the parser's order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Action {
    Tailscale,
    Off,
    Status,
}

impl Action {
    pub(super) fn name(self) -> &'static str {
        match self {
            Action::Tailscale => "tailscale",
            Action::Off => "off",
            Action::Status => "status",
        }
    }

    fn description(self) -> &'static str {
        match self {
            Action::Tailscale => "forward tailnet port <port> to the daemon on 127.0.0.1:<port>",
            Action::Off => "remove that forward (never anything else on the port)",
            Action::Status => "print the client URL, or why there is none",
        }
    }

    /// `--yes` exists on the actions that change something.
    fn changes(self) -> bool {
        !matches!(self, Action::Status)
    }

    /// The subparser's usage line.
    pub(super) fn usage(self) -> String {
        let yes = if self.changes() { " [--yes]" } else { "" };
        format!(
            "usage: pseudolife-mcp expose {} [-h] [--port PORT]{yes} [--json]\n",
            self.name()
        )
    }

    /// The subparser's help at `COLUMNS=80`.
    pub(super) fn help(self) -> String {
        let yes = if self.changes() {
            "  --yes        do not ask for confirmation\n"
        } else {
            ""
        };
        format!(
            "{}\n{}\n\n{OPTIONS}{yes}  --json       print a JSON report\n",
            self.usage(),
            self.description()
        )
    }
}

/// The main parser's usage block (`format_usage`).
pub(super) fn main_usage() -> &'static str {
    &MAIN_HELP[..MAIN_HELP
        .find("\n\n")
        .map_or(MAIN_HELP.len(), |end| end + 1)]
}

/// The parsed namespace.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Args {
    pub(super) action: Action,
    pub(super) port: u16,
    pub(super) yes: bool,
    pub(super) json: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Parsed {
    Args(Args),
    /// The main parser's help (stdout, exit 0).
    MainHelp,
    /// An action's help (stdout, exit 0).
    Help(Action),
    /// argparse's message after `prog: error: `, with the parser that
    /// reports it (`None` is the main parser), exit 2.
    Error(Option<Action>, String),
}

/// CPython 3.11's default `sys.get_int_max_str_digits()`.
const INT_MAX_STR_DIGITS: usize = 4300;

/// `_port(value)`: `Ok(Some(port))`, `Ok(None)` for argparse's
/// `not a port` error, `Err(())` where `int()` accepts a spelling this port
/// does not reproduce (a sign, whitespace, underscores). Anything but ASCII
/// is no port: `_port` refuses it before `int()`.
pub(super) fn port_value(value: &str) -> Result<Option<u16>, ()> {
    if !value.is_ascii() {
        return Ok(None);
    }
    if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) {
        // int()'s digit limit (4300; the native client ignores
        // PYTHONINTMAXSTRDIGITS) counts leading zeros: past it int()
        // raises, and `_port` reports that as no port.
        if value.len() > INT_MAX_STR_DIGITS {
            return Ok(None);
        }
        let digits = value.trim_start_matches('0');
        let port = if digits.len() > 5 {
            None
        } else {
            digits.parse::<u32>().ok().or(Some(0))
        };
        return Ok(port
            .filter(|port| (1..=65535).contains(port))
            .map(|port| port as u16));
    }
    if int_accepts(value) {
        return Err(());
    }
    Ok(None)
}

/// Whether CPython 3.11's `int(value)` (base 10) accepts an ASCII `value`:
/// surrounding whitespace, an optional sign, digits with single underscores
/// between them.
fn int_accepts(value: &str) -> bool {
    let space = |c: char| {
        matches!(
            c,
            ' ' | '\t' | '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}'..='\u{1f}'
        )
    };
    let body = value.trim_matches(space);
    let body = body.strip_prefix(['+', '-']).unwrap_or(body);
    !body.is_empty()
        && !body.starts_with('_')
        && !body.ends_with('_')
        && !body.contains("__")
        && body.bytes().all(|b| b.is_ascii_digit() || b == b'_')
}

fn option_error(option: &str, action: Option<Action>) -> String {
    let fix = ["--help", "--port", "--json", "--yes"]
        .into_iter()
        .find(|full| {
            option.starts_with("--")
                && option.len() > 2
                && option.len() < full.len()
                && full.starts_with(option)
                && (*full != "--yes" || action != Some(Action::Status))
        });
    let quoted = super::py_repr(option);
    match fix {
        Some(full) => format!("unrecognized option {quoted}; use {full} in full"),
        None => format!(
            "unrecognized option {quoted}; run pseudolife-mcp expose --help for supported options"
        ),
    }
}

fn explicit(option: &str, value: &str) -> String {
    format!(
        "argument {option}: ignored explicit argument {}",
        super::py_repr(value)
    )
}

/// The subparser's own pass over the words after the action.
fn parse_action(action: Action, rest: &[String]) -> Parsed {
    let mut args = Args {
        action,
        port: super::DEFAULT_PORT,
        yes: false,
        json: false,
    };
    let mut extras: Vec<&str> = Vec::new();
    let mut tokens = rest.iter().peekable();
    let error = |message: String| Parsed::Error(Some(action), message);
    while let Some(token) = tokens.next() {
        let token = token.as_str();
        if !token.starts_with('-') {
            extras.push(token);
            continue;
        }
        let (option, inline) = match token.split_once('=') {
            Some((option, value)) => (option, Some(value)),
            None => (token, None),
        };
        match (option, inline) {
            ("-h" | "--help", None) => return Parsed::Help(action),
            ("-h" | "--help", Some(_)) => return error("--help takes no value; use --help".into()),
            ("--json", None) => args.json = true,
            ("--yes", None) if action.changes() => args.yes = true,
            // `status` has no --yes: an unknown option, kept as an extra.
            ("--yes", None) => extras.push(token),
            ("--json", Some(value)) => return error(explicit("--json", value)),
            ("--yes", Some(value)) if action.changes() => return error(explicit("--yes", value)),
            ("--port", _) => {
                let value = match inline {
                    Some(value) => value,
                    None => match tokens.peek() {
                        None => {
                            return error("argument --port: expected one argument".into());
                        }
                        Some(value)
                            if value.starts_with("--")
                                || value.as_str() == "-h"
                                || value.starts_with("-h=") =>
                        {
                            return error(
                                "--port needs a value from 1 to 65535; use --port PORT".into(),
                            );
                        }
                        Some(_) => tokens.next().map(String::as_str).unwrap_or_default(),
                    },
                };
                if value.starts_with('-') {
                    return error("argument --port: use 1 to 65535 in plain digits".into());
                }
                match port_value(value) {
                    Ok(Some(port)) => args.port = port,
                    Ok(None) => {
                        return error(format!(
                            "argument --port: not a port: {}",
                            super::py_repr(value)
                        ));
                    }
                    Err(()) => {
                        return error("argument --port: use 1 to 65535 in plain digits".into());
                    }
                }
            }
            _ => return error(option_error(option, Some(action))),
        }
    }
    if !extras.is_empty() {
        return Parsed::Error(
            None,
            format!("unrecognized arguments: {}", extras.join(" ")),
        );
    }
    Parsed::Args(args)
}

pub(super) fn parse(argv: &[String]) -> Parsed {
    let Some((first, rest)) = argv.split_first() else {
        return Parsed::Error(None, "the following arguments are required: action".into());
    };
    let action = match first.as_str() {
        "-h" | "--help" => return Parsed::MainHelp,
        "tailscale" => Action::Tailscale,
        "off" => Action::Off,
        "status" => Action::Status,
        word if word.starts_with('-') => {
            let option = word.split_once('=').map_or(word, |(option, _)| option);
            let message = match option {
                "-h" | "--help" => "--help takes no value; use --help".into(),
                "--port" | "--yes" | "--json" => format!(
                    "option {} must follow its action; put it after {}",
                    super::py_repr(option),
                    if option == "--yes" {
                        "tailscale or off"
                    } else {
                        "tailscale, off or status"
                    }
                ),
                _ => option_error(option, None),
            };
            return Parsed::Error(None, message);
        }
        word => {
            return Parsed::Error(
                None,
                format!(
                    "argument action: invalid choice: {} (choose from 'tailscale', 'off', 'status')",
                    super::py_repr(word)
                ),
            );
        }
    };
    parse_action(action, rest)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(items: &[&str]) -> Parsed {
        parse(
            &items
                .iter()
                .map(|item| (*item).to_owned())
                .collect::<Vec<_>>(),
        )
    }

    fn args(action: Action, port: u16, yes: bool, json: bool) -> Parsed {
        Parsed::Args(Args {
            action,
            port,
            yes,
            json,
        })
    }

    #[test]
    fn canonical_argv_parses_with_the_last_port_winning() {
        assert_eq!(run(&["status"]), args(Action::Status, 8765, false, false));
        assert_eq!(
            run(&[
                "tailscale",
                "--port",
                "9",
                "--yes",
                "--json",
                "--port=08443"
            ]),
            args(Action::Tailscale, 8443, true, true)
        );
        assert_eq!(
            run(&["off", "--json"]),
            args(Action::Off, 8765, false, true)
        );
    }

    #[test]
    fn errors_come_in_argparse_order() {
        assert_eq!(
            run(&[]),
            Parsed::Error(None, "the following arguments are required: action".into())
        );
        assert_eq!(run(&["-h", "bogus"]), Parsed::MainHelp);
        assert!(matches!(run(&["bogus", "-h"]), Parsed::Error(None, m) if m.contains("'bogus'")));
        assert_eq!(run(&["status", "zz", "-h"]), Parsed::Help(Action::Status));
        assert_eq!(
            run(&["off", "--port", "70000", "-h"]),
            Parsed::Error(
                Some(Action::Off),
                "argument --port: not a port: '70000'".into()
            )
        );
        assert_eq!(
            run(&["status", "--yes"]),
            Parsed::Error(None, "unrecognized arguments: --yes".into())
        );
        assert_eq!(
            run(&["off", "a", "b c"]),
            Parsed::Error(None, "unrecognized arguments: a b c".into())
        );
        assert_eq!(
            run(&["tailscale", "zz", "--port", "x"]),
            Parsed::Error(
                Some(Action::Tailscale),
                "argument --port: not a port: 'x'".into()
            )
        );
        assert_eq!(
            run(&["off", "--yes=1"]),
            Parsed::Error(
                Some(Action::Off),
                "argument --yes: ignored explicit argument '1'".into()
            )
        );
        assert_eq!(
            run(&["tailscale", "--port"]),
            Parsed::Error(
                Some(Action::Tailscale),
                "argument --port: expected one argument".into()
            )
        );
    }

    #[test]
    fn port_values_follow_int_and_the_range() {
        assert_eq!(port_value("8765"), Ok(Some(8765)));
        assert_eq!(port_value("000080"), Ok(Some(80)));
        assert_eq!(port_value("65535"), Ok(Some(65535)));
        // int()'s digit limit counts leading zeros: 4300 digits are read,
        // 4301 raise, which `_port` reports as no port.
        assert_eq!(port_value(&format!("{}80", "0".repeat(4298))), Ok(Some(80)));
        assert_eq!(port_value(&format!("{}80", "0".repeat(4299))), Ok(None));
        for refused in [
            "0",
            "65536",
            "",
            "x",
            "8x",
            "it's",
            "99999999999999999999",
            "1_",
            "_1",
            "\u{0668}0",
            "\u{ff18}\u{ff17}\u{ff16}\u{ff15}",
            "8765\u{a0}",
        ] {
            assert_eq!(port_value(refused), Ok(None), "{refused}");
        }
        for other in ["+80", "-1", " 80", "8_765"] {
            assert_eq!(port_value(other), Err(()), "{other}");
        }
    }

    #[test]
    fn noncanonical_arguments_refuse_before_host_access() {
        for (argv, fix) in [
            (vec!["--he"], "use --help in full"),
            (vec!["--port", "9", "status"], "after"),
            (vec!["status", "--po", "8765"], "--port"),
            (vec!["status", "--"], "--help"),
            (vec!["tailscale", "--port", "-1"], "plain digits"),
            (vec!["status", "-x"], "--help"),
            (vec!["status", "--yes=1"], "--help"),
            (vec!["off", "--port", "+8765"], "plain digits"),
            (vec!["off", "--port", " 80"], "plain digits"),
            (vec!["off", "--port", "8_0"], "plain digits"),
            (vec!["status", "--zz"], "--help"),
        ] {
            assert!(
                matches!(run(&argv), Parsed::Error(_, m) if m.contains(fix)),
                "{argv:?}"
            );
        }
        assert_eq!(run(&["--help"]), Parsed::MainHelp);
        assert_eq!(
            run(&["status", "--port", "80"]),
            args(Action::Status, 80, false, false)
        );
        assert_eq!(run(&["off", "--yes"]), args(Action::Off, 8765, true, false));
    }

    #[test]
    fn option_repairs_are_supported_by_the_selected_action() {
        for option in ["--y", "--ye"] {
            assert!(matches!(run(&["status", option]), Parsed::Error(_, m)
                if m.contains("--help") && !m.contains("use --yes")));
            assert!(matches!(run(&["off", option]), Parsed::Error(_, m)
                if m.contains("use --yes in full")));
            assert!(matches!(run(&["tailscale", option]), Parsed::Error(_, m)
                if m.contains("use --yes in full")));
        }
        assert_eq!(run(&["off", "--yes"]), args(Action::Off, 8765, true, false));
        assert_eq!(run(&["status", "--help"]), Parsed::Help(Action::Status));
    }

    #[test]
    fn misplaced_options_name_the_position_and_one_repair() {
        for option in ["--port", "--yes", "--json", "--port=9"] {
            let Parsed::Error(None, message) = run(&[option, "9", "status"]) else {
                panic!("usage expected")
            };
            assert!(message.contains("must follow"), "{message}");
            assert!(!message.contains("unrecognized"), "{message}");
            assert!(!message.contains("--help"), "{message}");
        }
        let Parsed::Error(None, message) = run(&["--he"]) else {
            panic!("usage expected")
        };
        assert!(message.contains("use --help in full"));
        assert!(!message.contains("after") && !message.contains("follow"));
        assert_eq!(run(&["--help"]), Parsed::MainHelp);
        assert_eq!(
            run(&["status", "--port", "9"]),
            args(Action::Status, 9, false, false)
        );
    }

    #[test]
    fn help_with_an_explicit_value_names_the_flag_error() {
        for argv in [
            vec!["--help=1"],
            vec!["-h=1"],
            vec!["status", "--help=1"],
            vec!["off", "-h=1"],
        ] {
            assert!(
                matches!(run(&argv), Parsed::Error(_, m) if m.contains("--help takes no value"))
            );
        }
        assert_eq!(run(&["off", "--help"]), Parsed::Help(Action::Off));
    }

    #[test]
    fn a_port_followed_by_an_option_needs_a_value() {
        for option in ["--yes", "--json", "--help", "-h"] {
            assert!(
                matches!(run(&["tailscale", "--port", option]), Parsed::Error(_, m)
                if m.contains("--port needs a value") && m.contains("1 to 65535"))
            );
        }
        assert_eq!(
            run(&["tailscale", "--port", "80", "--yes"]),
            args(Action::Tailscale, 80, true, false)
        );
        for value in ["--port=-x", "--port=-"] {
            assert!(
                matches!(run(&["tailscale", value]), Parsed::Error(_, m) if m.contains("plain digits"))
            );
        }
    }

    #[test]
    fn unknown_options_escape_control_characters() {
        for argv in [vec!["--bad\u{1b}x"], vec!["status", "--bad\u{1b}x"]] {
            let Parsed::Error(_, message) = run(&argv) else {
                panic!("usage expected")
            };
            assert!(!message.contains('\u{1b}'));
            assert!(message.contains("'--bad\\x1bx'"));
        }
        assert_eq!(
            run(&["status", "--json"]),
            args(Action::Status, 8765, false, true)
        );
    }

    #[test]
    fn help_texts_follow_the_parser() {
        assert!(main_usage().ends_with("{tailscale,off,status} ...\n"));
        assert_eq!(main_usage().lines().count(), 1);
        assert_eq!(
            Action::Status.usage(),
            "usage: pseudolife-mcp expose status [-h] [--port PORT] [--json]\n"
        );
        assert!(
            Action::Off
                .help()
                .contains("  --yes        do not ask for confirmation\n")
        );
        assert!(!Action::Status.help().contains("--yes"));
    }
}
