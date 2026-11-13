//! `conductor_watch_cli.parser().parse_args` for the argv this port answers.
//!
//! Admitted tokens: every option spelled in full, `-h`, `--help`,
//! `--opt=V` (on a flag, argparse's "ignored explicit argument" error), a
//! value as the next argument when it does not start with `-`, positional
//! words and unknown `--words` that no option starts with (argparse's
//! "unrecognized arguments" error). Options act in order, as argparse's do:
//! help exits where it is met, a missing value or a refused type errors
//! there, extras are reported once every option is consumed. Repeats are
//! argparse's: the last value wins, `--lease` appends. An abbreviation, any
//! other token starting with `-`, `--`, a value starting with `-` after a
//! space, an unknown `--word=V`, a number spelling outside plain ASCII
//! decimal, `inf`, `infinity` and `nan` (`1_0`, spaces, other digits) and
//! a Windows `--state` starting with two separators defer.

/// `parser().format_help()` at `COLUMNS=80` (argparse wraps at 78).
pub(super) const HELP: &str = include_str!("help.txt");

/// The usage block argparse prints before an error (`format_usage`).
pub(super) fn usage() -> &'static str {
    &HELP[..HELP.find("\n\n").map_or(HELP.len(), |end| end + 1)]
}

/// The parsed namespace: only the fields the answered shapes read.
#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct Args {
    pub(super) owner: Option<String>,
    pub(super) detach: bool,
    pub(super) stop: bool,
    pub(super) state: Option<String>,
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

const FLAGS: [&str; 2] = ["--detach", "--stop"];
/// Options taking one value, with the type check argparse applies.
const VALUES: [(&str, Kind); 13] = [
    ("--owner", Kind::Text),
    ("--until-lease", Kind::Text),
    ("--repo", Kind::Text),
    ("--author", Kind::Text),
    ("--quiet-after", Kind::Positive),
    ("--max-age", Kind::Age),
    ("--interval", Kind::Positive),
    ("--settle", Kind::NonNegative),
    ("--state", Kind::State),
    ("--suite-results", Kind::Text),
    ("--lease", Kind::Text),
    ("--lease-cli", Kind::Text),
    // SUPPRESSed from the help, still an option.
    ("--nonce", Kind::Text),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Text,
    Positive,
    NonNegative,
    Age,
    /// `_state_path`: a path whose pathlib name is a file name.
    State,
}

fn kind(option: &str) -> Option<Kind> {
    VALUES
        .iter()
        .find(|(name, _)| *name == option)
        .map(|(_, kind)| *kind)
}

/// Every option string, for argparse's prefix (abbreviation) matching.
fn known() -> impl Iterator<Item = &'static str> {
    ["--help"]
        .into_iter()
        .chain(FLAGS)
        .chain(VALUES.iter().map(|(name, _)| *name))
}

/// `float(text)` over the spellings decided here: `Ok(Some(value))`, a
/// refusal (`Ok(None)`: argparse's "invalid ... value"), or `Err(())` for a
/// spelling whose fate this port does not reproduce.
fn python_float(text: &str) -> Result<Option<f64>, ()> {
    if text.is_empty() {
        return Ok(None);
    }
    if !text
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'+' | b'-'))
    {
        return Err(());
    }
    let unsigned = text.strip_prefix(['+', '-']).unwrap_or(text);
    let negative = text.starts_with('-');
    if unsigned.eq_ignore_ascii_case("nan") {
        return Ok(Some(f64::NAN));
    }
    if ["inf", "infinity"]
        .iter()
        .any(|word| unsigned.eq_ignore_ascii_case(word))
    {
        return Ok(Some(if negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        }));
    }
    let (mantissa, exponent) = unsigned
        .split_once(['e', 'E'])
        .map_or((unsigned, None), |(left, right)| (left, Some(right)));
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let digits = |part: &str| part.bytes().all(|b| b.is_ascii_digit());
    let mantissa_ok = digits(whole)
        && digits(fraction)
        && !(whole.is_empty() && fraction.is_empty())
        && mantissa.matches('.').count() <= 1;
    let exponent_ok = exponent.is_none_or(|exponent| {
        let exponent = exponent.strip_prefix(['+', '-']).unwrap_or(exponent);
        !exponent.is_empty() && digits(exponent)
    });
    if !(mantissa_ok && exponent_ok) {
        return Ok(None);
    }
    text.parse::<f64>().map(Some).map_err(|_| ())
}

/// CPython applies its integer digit limit only to texts longer than this
/// (`sys.int_info.str_digits_check_threshold`).
const INT_LIMIT_FREE: usize = 640;

/// `lease_cli.parse_duration` then `_age`'s check. Its pattern is ASCII
/// (`re.ASCII`), so a non-ASCII digit or unit letter is no duration.
fn age(text: &str) -> Result<Result<(), String>, ()> {
    let stripped = super::super::pyjson::py_strip(text);
    // Past this length CPython's integer digit limit (4300 by default,
    // moved by PYTHONINTMAXSTRDIGITS) may refuse the count: not decided here.
    if stripped.len() > INT_LIMIT_FREE {
        return Err(());
    }
    // `([0-9]+)([smhd]?)` under IGNORECASE: the unit is one optional letter.
    let number = stripped
        .strip_suffix(|c: char| "smhdSMHD".contains(c))
        .unwrap_or(stripped);
    if number.is_empty() || !number.bytes().all(|b| b.is_ascii_digit()) {
        return Ok(Err(format!(
            "not a duration: {} (use 90, 90s, 20m, 2h or 7d)",
            super::py_repr(text)
        )));
    }
    // Any multiple of zero seconds is zero; any other count is above it.
    if number.bytes().all(|b| b == b'0') {
        return Ok(Err("must be above 0".into()));
    }
    Ok(Ok(()))
}

/// argparse's type call: `Ok(None)` accepts, `Ok(Some(message))` refuses.
fn check(option: &str, kind: Kind, value: &str) -> Result<Option<String>, ()> {
    let refused = |what: &str| Some(format!("argument {option}: {what}"));
    Ok(match kind {
        Kind::Text => None,
        Kind::Positive | Kind::NonNegative => {
            let name = if kind == Kind::Positive {
                "_positive"
            } else {
                "_non_negative"
            };
            match python_float(value)? {
                None => refused(&format!("invalid {name} value: {}", super::py_repr(value))),
                // `not value > 0` and `not value >= 0`: NaN fails both.
                Some(number) if kind == Kind::Positive && (number.is_nan() || number <= 0.0) => {
                    refused("must be above 0")
                }
                Some(number) if kind == Kind::NonNegative && (number.is_nan() || number < 0.0) => {
                    refused("must be 0 or more")
                }
                Some(_) => None,
            }
        }
        Kind::Age => match age(value)? {
            Ok(()) => None,
            Err(message) => refused(&message),
        },
        Kind::State => match path_name(value)? {
            "" | "." | ".." => refused(&format!(
                "{} has no file name; give a file path such as \
                 ~/.pseudolife-mcp/watch/<owner>.json",
                super::py_repr(value)
            )),
            _ => None,
        },
    })
}

/// `Path(text).name` (CPython 3.11 pathlib): the last component after the
/// drive and root, `""` when there is none. pathlib drops empty and `.`
/// components. On Windows `/` also separates and `X:` (an ASCII letter) is
/// a drive; a text starting with two separators (UNC, `\\?\`, `\\.\`) has
/// drive rules not reproduced here, so it defers.
fn path_name(text: &str) -> Result<&str, ()> {
    let separator = |c: char| c == '/' || (cfg!(windows) && c == '\\');
    let mut rest = text;
    if cfg!(windows) {
        if rest.starts_with(separator) && rest[1..].starts_with(separator) {
            return Err(());
        }
        let bytes = rest.as_bytes();
        if bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
            rest = &rest[2..];
        }
    }
    Ok(rest
        .split(separator)
        .rfind(|part| !part.is_empty() && *part != ".")
        .unwrap_or(""))
}

fn store(args: &mut Args, option: &str, value: &str) {
    match option {
        "--owner" => args.owner = Some(value.to_owned()),
        "--state" => args.state = Some(value.to_owned()),
        _ => {}
    }
}

/// Whether argparse classifies `token` in a way this port reproduces: a
/// word, an option spelled in full (alone or as `--opt=V`), or an unknown
/// `--word` no option starts with.
fn classified(token: &str) -> bool {
    if !token.starts_with('-')
        || matches!(token, "-h" | "--help")
        || FLAGS.contains(&token)
        || kind(token).is_some()
    {
        return true;
    }
    if let Some((option, _)) = token.split_once('=') {
        return kind(option).is_some() || FLAGS.contains(&option);
    }
    token.len() > 2
        && token.starts_with("--")
        && !token.contains(' ')
        && !known().any(|option| option.starts_with(token))
}

/// argparse's consumption order: options act as they are met; words
/// collect as extras, reported once every option has been consumed.
pub(super) fn parse(argv: &[String]) -> Parsed {
    // argparse classifies every argument before it runs any action, so a
    // token it settles its own way (an abbreviation, an ambiguous prefix,
    // `--`, `-x`, an unknown `--word=V`) defers the whole argv, even after
    // a --help or a type error that would otherwise act first.
    if !argv.iter().all(|token| classified(token)) {
        return Parsed::Defer;
    }
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
            if token == "--stop" {
                args.stop = true;
            } else {
                args.detach = true;
            }
            continue;
        }
        if let Some(kind) = kind(token) {
            let value = match tokens.peek() {
                Some(value) if !value.starts_with('-') => value.as_str(),
                None => return Parsed::Error(format!("argument {token}: expected one argument")),
                Some(_) => return Parsed::Defer,
            };
            tokens.next();
            match check(token, kind, value) {
                Err(()) => return Parsed::Defer,
                Ok(Some(message)) => return Parsed::Error(message),
                Ok(None) => store(&mut args, token, value),
            }
            continue;
        }
        if let Some((option, value)) = token.split_once('=') {
            if let Some(kind) = kind(option) {
                match check(option, kind, value) {
                    Err(()) => return Parsed::Defer,
                    Ok(Some(message)) => return Parsed::Error(message),
                    Ok(None) => store(&mut args, option, value),
                }
                continue;
            }
            if FLAGS.contains(&option) {
                return Parsed::Error(format!(
                    "argument {option}: ignored explicit argument {}",
                    super::py_repr(value)
                ));
            }
            return Parsed::Defer;
        }
        // An unknown `--word` no option starts with is an extra; a prefix
        // of one is an abbreviation (or ambiguous), argparse's business.
        if token.len() > 2
            && token.starts_with("--")
            && !token.contains(' ')
            && !known().any(|option| option.starts_with(token))
        {
            extras.push(token);
            continue;
        }
        return Parsed::Defer;
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

    fn error(text: &str) -> Parsed {
        Parsed::Error(text.into())
    }

    #[test]
    fn a_token_argparse_settles_itself_defers_before_help_or_an_error() {
        // argparse classifies every argument before it runs any action: a
        // later abbreviation or ambiguous prefix is settled (or refused)
        // before an earlier --help or type error acts.
        for argv in [
            &["--help", "--s"][..],
            &["-h", "--sto"],
            &["--interval", "x", "--s"],
            &["--help", "--zz=1"],
            &["zz", "--help", "-x"],
            &["--stop=1", "--"],
        ] {
            assert_eq!(run(argv), Parsed::Defer, "{argv:?}");
        }
        // Options spelled in full and unknown words still let help act.
        assert_eq!(run(&["--help", "--stop", "--zz", "word"]), Parsed::Help);
    }

    #[test]
    fn a_duration_long_enough_for_the_integer_digit_limit_defers() {
        let long = "1".repeat(641);
        assert_eq!(run(&["--max-age", &long]), Parsed::Defer);
        assert!(matches!(
            run(&["--max-age", &"1".repeat(640)]),
            Parsed::Args(_)
        ));
    }

    #[test]
    fn canonical_argv_parses_with_the_last_value_winning() {
        assert_eq!(
            run(&[
                "--owner",
                "a",
                "--stop",
                "--state=s",
                "--owner=b",
                "--lease",
                "x",
                "--lease=y",
                "--max-age",
                " 2H ",
                "--interval",
                "1e-3",
                "--settle",
                "0",
                "--nonce",
                "n",
            ]),
            Parsed::Args(Args {
                owner: Some("b".into()),
                stop: true,
                state: Some("s".into()),
                ..Args::default()
            })
        );
        assert_eq!(
            run(&["--detach", "--state", "a/."]),
            Parsed::Args(Args {
                detach: true,
                state: Some("a/.".into()),
                ..Args::default()
            })
        );
    }

    fn no_file_name(text: &str) -> Parsed {
        error(&format!(
            "argument --state: {} has no file name; give a file path such as \
             ~/.pseudolife-mcp/watch/<owner>.json",
            super::super::py_repr(text)
        ))
    }

    #[test]
    fn a_state_with_no_file_name_is_refused_where_argparse_meets_it() {
        assert!(matches!(run(&["--state", "x//./"]), Parsed::Args(_)));
        for text in ["", ".", "..", "/", "a/..", "./."] {
            assert_eq!(run(&["--state", text]), no_file_name(text), "{text:?}");
            assert_eq!(
                run(&[&format!("--state={text}")]),
                no_file_name(text),
                "{text:?}"
            );
        }
        // In parse order: before the extras, after an earlier type error.
        assert_eq!(
            run(&["--state", "x", "--state", ".", "--bogus"]),
            no_file_name(".")
        );
        assert_eq!(
            run(&["--settle", "x", "--state", ""]),
            error("argument --settle: invalid _non_negative value: 'x'")
        );
        if cfg!(windows) {
            for text in ["C:", "C:\\", "c:..\\.", "\\.", "a\\..", "C:x\\..", "x/\\.."] {
                assert_eq!(run(&["--state", text]), no_file_name(text), "{text:?}");
            }
            for text in ["C:x", "1:", "a:b", "C:\\w\\s.json"] {
                assert!(
                    matches!(run(&["--state", text]), Parsed::Args(_)),
                    "{text:?}"
                );
            }
            // UNC and extended forms: pathlib's drive rules, not reproduced.
            for text in ["\\\\srv\\share", "//srv/share/x", "\\\\?\\C:"] {
                assert_eq!(run(&["--state", text]), Parsed::Defer, "{text:?}");
            }
        } else {
            for text in ["a\\..", "C:", "//srv/share"] {
                assert!(
                    matches!(run(&["--state", text]), Parsed::Args(_)),
                    "{text:?}"
                );
            }
        }
    }

    #[test]
    fn nan_and_infinity_are_read_as_float_reads_them() {
        for argv in [
            &["--settle", "nan"][..],
            &["--settle=-NaN"],
            &["--settle=+nan"],
        ] {
            assert_eq!(
                run(argv),
                error("argument --settle: must be 0 or more"),
                "{argv:?}"
            );
        }
        assert_eq!(
            run(&["--interval", "nan"]),
            error("argument --interval: must be above 0")
        );
        assert_eq!(
            run(&["--quiet-after=-inf"]),
            error("argument --quiet-after: must be above 0")
        );
        for argv in [
            &["--settle", "inf"][..],
            &["--quiet-after", "Infinity"],
            &["--interval", "+INF"],
            &["--settle", "1e999"],
        ] {
            assert!(matches!(run(argv), Parsed::Args(_)), "{argv:?}");
        }
        assert_eq!(
            run(&["--settle", "nanx"]),
            error("argument --settle: invalid _non_negative value: 'nanx'")
        );
        assert_eq!(run(&["--settle", "nan(1)"]), Parsed::Defer);
    }

    #[test]
    fn a_duration_is_ascii_digits_and_an_ascii_unit() {
        for text in ["5\u{17f}", "\u{665}", "5\u{212a}"] {
            assert_eq!(
                run(&["--max-age", text]),
                error(&format!(
                    "argument --max-age: not a duration: {} (use 90, 90s, 20m, 2h or 7d)",
                    super::super::py_repr(text)
                )),
                "{text:?}"
            );
        }
        // str.strip() takes Unicode whitespace.
        assert!(matches!(
            run(&["--max-age", "\u{a0}5m\u{a0}"]),
            Parsed::Args(_)
        ));
    }

    #[test]
    fn type_checks_say_what_argparse_says() {
        assert_eq!(
            run(&["--quiet-after", "0"]),
            error("argument --quiet-after: must be above 0")
        );
        assert_eq!(
            run(&["--interval=-0"]),
            error("argument --interval: must be above 0")
        );
        assert_eq!(
            run(&["--quiet-after", "soon"]),
            error("argument --quiet-after: invalid _positive value: 'soon'")
        );
        assert_eq!(
            run(&["--settle=-0.5"]),
            error("argument --settle: must be 0 or more")
        );
        assert_eq!(run(&["--settle=-0"]), Parsed::Args(Args::default()));
        assert_eq!(
            run(&["--settle", "1.2.3"]),
            error("argument --settle: invalid _non_negative value: '1.2.3'")
        );
        for bad in ["", ".", "e5", "1e", "1e+", "+", "5x"] {
            assert_eq!(
                run(&[&format!("--interval={bad}")]),
                error(&format!(
                    "argument --interval: invalid _positive value: {}",
                    super::super::py_repr(bad)
                )),
                "{bad}"
            );
        }
        assert_eq!(
            run(&["--max-age", "0d"]),
            error("argument --max-age: must be above 0")
        );
        assert_eq!(
            run(&["--max-age", "5x"]),
            error("argument --max-age: not a duration: '5x' (use 90, 90s, 20m, 2h or 7d)")
        );
        assert_eq!(
            run(&["--max-age=", "--bogus"]),
            error("argument --max-age: not a duration: '' (use 90, 90s, 20m, 2h or 7d)")
        );
        assert_eq!(
            run(&["--max-age", "it's"]),
            error("argument --max-age: not a duration: \"it's\" (use 90, 90s, 20m, 2h or 7d)")
        );
    }

    #[test]
    fn errors_come_in_argparse_order() {
        assert_eq!(run(&["--stop", "-h", "--owner"]), Parsed::Help);
        assert_eq!(
            run(&["zz", "--owner"]),
            error("argument --owner: expected one argument")
        );
        assert_eq!(
            run(&["a", "--stop", "b", "--bogus"]),
            error("unrecognized arguments: a b --bogus")
        );
        assert_eq!(run(&["zz", "--bogus", "--help"]), Parsed::Help);
        assert_eq!(
            run(&["--interval", "x", "--bogus", "-h"]),
            error("argument --interval: invalid _positive value: 'x'")
        );
        assert_eq!(
            run(&["--detach=1"]),
            error("argument --detach: ignored explicit argument '1'")
        );
        assert_eq!(
            run(&["--stop="]),
            error("argument --stop: ignored explicit argument ''")
        );
    }

    #[test]
    fn argparse_corners_defer() {
        for argv in [
            &["--sto"][..],
            &["--own", "x"],
            &["--s"],
            &["--lease-c=x"],
            &["--state", "-x"],
            &["--owner", "--stop"],
            &["--"],
            &["-x"],
            &["-"],
            &["--help=x"],
            &["--zz=1"],
            &["--a b"],
            &["--settle", "-nan"],
            &["--settle", "1_0"],
            &["--settle", " 1"],
            &["--interval", "\u{661}"],
        ] {
            assert_eq!(run(argv), Parsed::Defer, "{argv:?}");
        }
    }

    #[test]
    fn usage_is_the_help_head() {
        assert!(usage().starts_with("usage: pseudolife-mcp conductor-watch [-h]"));
        assert!(usage().ends_with("[--lease LEASE] [--lease-cli LEASE_CLI]\n"));
        assert_eq!(usage().lines().count(), 9);
    }
}
