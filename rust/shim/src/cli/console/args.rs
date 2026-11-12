//! `console_cli._parser().parse_args` for the argv this port answers.
//!
//! Admitted tokens: the six options spelled in full, `-h`, `--help`,
//! `--to=V` / `--revoke=V`, `--opt=V` on a flag (argparse's "ignored
//! explicit argument" error), a value as the next argument when it does not
//! start with `-`, and positional words (argparse's "unrecognized
//! arguments" error). Repeats are argparse's: the last value wins. An
//! abbreviation, any other token starting with `-`, `--`, and a value
//! starting with `-` are argparse's own business and defer.

/// `_parser().format_help()` at `COLUMNS=80` (argparse wraps at 78).
pub(super) const HELP: &str = concat!(
    "usage: pseudolife-mcp console [-h] [--no-open] [--to PAGE] [--list]\n",
    "                              [--revoke SESSION_ID] [--unless-signed-in]\n",
    "                              [--json]\n",
    "\n",
    "Open the Console in a browser, signed in. The link lasts 5 minutes and works\n",
    "once.\n",
    "\n",
    "options:\n",
    "  -h, --help           show this help message and exit\n",
    "  --no-open            print the link and the code without opening a browser\n",
    "  --to PAGE            the Console page to land on, such as board\n",
    "  --list               list the browsers signed in to this bank\n",
    "  --revoke SESSION_ID  sign one browser out (an id from --list)\n",
    "  --unless-signed-in   do nothing when a browser is already signed in to this\n",
    "                       bank (what install runs, so a rerun opens no new tab)\n",
    "  --json               print one JSON object\n",
);

/// The usage block argparse prints before an error (`format_usage`).
pub(super) fn usage() -> &'static str {
    &HELP[..HELP.find("\n\n").map_or(HELP.len(), |end| end + 1)]
}

/// The parsed namespace.
#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct Args {
    pub(super) no_open: bool,
    pub(super) to: Option<String>,
    pub(super) list: bool,
    pub(super) revoke: Option<String>,
    pub(super) unless_signed_in: bool,
    pub(super) json: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Parsed {
    Args(Args),
    /// `-h` / `--help`: the help on stdout, exit 0.
    Help,
    /// argparse's error message (after `prog: error: `), exit 2.
    Error(String),
    Defer,
}

const FLAGS: [&str; 4] = ["--no-open", "--list", "--unless-signed-in", "--json"];

fn set_flag(args: &mut Args, flag: &str) {
    match flag {
        "--no-open" => args.no_open = true,
        "--list" => args.list = true,
        "--unless-signed-in" => args.unless_signed_in = true,
        _ => args.json = true,
    }
}

fn set_value(args: &mut Args, option: &str, value: &str) {
    if option == "--to" {
        args.to = Some(value.to_owned());
    } else {
        args.revoke = Some(value.to_owned());
    }
}

/// argparse's consumption order: options act as they are met (help exits
/// there, a missing or explicit argument errors there); words collect as
/// extras, reported once every option has been consumed.
pub(super) fn parse(argv: &[String]) -> Parsed {
    let mut args = Args::default();
    let mut extras: Vec<&str> = Vec::new();
    let mut tokens = argv.iter().peekable();
    while let Some(token) = tokens.next() {
        let token = token.as_str();
        if !token.starts_with('-') {
            extras.push(token);
            continue;
        }
        if matches!(token, "-h" | "--help") {
            return Parsed::Help;
        }
        if FLAGS.contains(&token) {
            set_flag(&mut args, token);
            continue;
        }
        if matches!(token, "--to" | "--revoke") {
            match tokens.peek() {
                Some(value) if !value.starts_with('-') => {
                    set_value(&mut args, token, value);
                    tokens.next();
                }
                None => return Parsed::Error(format!("argument {token}: expected one argument")),
                Some(_) => return Parsed::Defer,
            }
            continue;
        }
        let Some((option, value)) = token.split_once('=') else {
            return Parsed::Defer;
        };
        if matches!(option, "--to" | "--revoke") {
            set_value(&mut args, option, value);
        } else if FLAGS.contains(&option) {
            return Parsed::Error(format!(
                "argument {option}: ignored explicit argument {}",
                super::py_repr(value)
            ));
        } else {
            return Parsed::Defer;
        }
    }
    if !extras.is_empty() {
        return Parsed::Error(format!("unrecognized arguments: {}", extras.join(" ")));
    }
    Parsed::Args(args)
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

    #[test]
    fn canonical_argv_parses_with_the_last_value_winning() {
        assert_eq!(
            run(&["--no-open", "--to", "board", "--json", "--to=agents"]),
            Parsed::Args(Args {
                no_open: true,
                to: Some("agents".into()),
                json: true,
                ..Args::default()
            })
        );
        assert_eq!(
            run(&["--revoke=", "--list"]),
            Parsed::Args(Args {
                list: true,
                revoke: Some(String::new()),
                ..Args::default()
            })
        );
        assert_eq!(
            run(&["--to", ""]),
            Parsed::Args(Args {
                to: Some(String::new()),
                ..Args::default()
            })
        );
    }

    #[test]
    fn errors_come_in_argparse_order() {
        assert_eq!(run(&["--list", "-h", "--to"]), Parsed::Help);
        assert_eq!(
            run(&["zz", "--to"]),
            Parsed::Error("argument --to: expected one argument".into())
        );
        assert_eq!(
            run(&["a", "--list", "b"]),
            Parsed::Error("unrecognized arguments: a b".into())
        );
        assert_eq!(run(&["zz", "--help"]), Parsed::Help);
        assert_eq!(
            run(&["--json=1"]),
            Parsed::Error("argument --json: ignored explicit argument '1'".into())
        );
        assert_eq!(
            run(&["--no-open="]),
            Parsed::Error("argument --no-open: ignored explicit argument ''".into())
        );
    }

    #[test]
    fn argparse_corners_defer() {
        for argv in [
            &["--li"][..],
            &["--to", "-x"],
            &["--to", "-5"],
            &["--revoke", "--list"],
            &["--"],
            &["-x"],
            &["--help=x"],
            &["--zz"],
            &["--zz=1"],
            &["-"],
        ] {
            assert_eq!(run(argv), Parsed::Defer, "{argv:?}");
        }
    }

    #[test]
    fn usage_is_the_help_head() {
        assert!(usage().ends_with("[--json]\n"));
        assert_eq!(usage().lines().count(), 3);
    }
}
