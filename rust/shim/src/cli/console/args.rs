//! Native Console options. Value options accept either spelling and the last wins.

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
    /// A usage error (exit 2).
    Error(String),
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

const UNRECOGNIZED: &str = "unrecognized argument; see --help for supported options";

/// Options act in order. Report unexpected positional arguments after options.
pub(super) fn parse(argv: &[String]) -> Parsed {
    let mut args = Args::default();
    let mut extra = false;
    let mut tokens = argv.iter().peekable();
    while let Some(token) = tokens.next() {
        let token = token.as_str();
        if token == "--" {
            extra |= tokens.next().is_some();
            break;
        }
        if !token.starts_with('-') || token == "-" {
            extra = true;
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
                _ => return Parsed::Error(format!("argument {token}: expected one argument")),
            }
            continue;
        }
        let Some((option, value)) = token.split_once('=') else {
            return Parsed::Error(UNRECOGNIZED.into());
        };
        if matches!(option, "--to" | "--revoke") {
            set_value(&mut args, option, value);
        } else if FLAGS.contains(&option) || matches!(option, "-h" | "--help") {
            return Parsed::Error(format!("argument {option}: pass this flag without a value"));
        } else {
            return Parsed::Error(UNRECOGNIZED.into());
        }
    }
    if extra {
        return Parsed::Error(UNRECOGNIZED.into());
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
            Parsed::Error(UNRECOGNIZED.into())
        );
        assert_eq!(run(&["zz", "--help"]), Parsed::Help);
        assert_eq!(
            run(&["--json=1"]),
            Parsed::Error("argument --json: pass this flag without a value".into())
        );
        assert_eq!(
            run(&["--no-open="]),
            Parsed::Error("argument --no-open: pass this flag without a value".into())
        );
    }

    #[test]
    fn malformed_options_name_the_fix_without_echoing_values() {
        let secret = "fixture-password-do-not-print";
        for (argv, expected) in [
            (
                &["--li"][..],
                "unrecognized argument; see --help for supported options",
            ),
            (
                &["-x"][..],
                "unrecognized argument; see --help for supported options",
            ),
            (
                &["--unknown=fixture-password-do-not-print"][..],
                "unrecognized argument; see --help for supported options",
            ),
            (
                &[secret][..],
                "unrecognized argument; see --help for supported options",
            ),
            (&["--to", "-x"][..], "argument --to: expected one argument"),
            (
                &["--revoke", "--list"][..],
                "argument --revoke: expected one argument",
            ),
            (
                &["--help=fixture-password-do-not-print"][..],
                "argument --help: pass this flag without a value",
            ),
        ] {
            let result = run(argv);
            assert!(
                result == Parsed::Error(expected.into()),
                "expected {expected}"
            );
        }
        assert_eq!(
            run(&["--list", "--"]),
            Parsed::Args(Args {
                list: true,
                ..Args::default()
            })
        );
        assert_eq!(
            run(&["--to=board"]),
            Parsed::Args(Args {
                to: Some("board".into()),
                ..Args::default()
            })
        );
    }

    #[test]
    fn usage_is_the_help_head() {
        assert!(usage().ends_with("[--json]\n"));
        assert_eq!(usage().lines().count(), 3);
    }
}
