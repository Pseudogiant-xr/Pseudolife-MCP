use super::{out, repr};

#[derive(Debug)]
pub struct Args {
    pub action: String,
    pub name: Option<String>,
    pub json: bool,
    pub no_board: bool,
    pub ttl: u64,
    pub expect: Option<u64>,
    pub timeout: Option<f64>,
    pub purpose: Option<String>,
    pub command: Vec<String>,
    pub while_pid: Option<u32>,
    pub worktree: String,
    pub agent: Option<String>,
    pub hold: u64,
}

fn asset(action: &str, help: bool) -> &'static str {
    match (action, help) {
        ("hold", true) => include_str!("assets/hold_help.txt"),
        ("hold", false) => include_str!("assets/hold_usage.txt"),
        ("break", true) => include_str!("assets/break_help.txt"),
        ("break", false) => include_str!("assets/break_usage.txt"),
        ("delegate", true) => include_str!("assets/delegate_help.txt"),
        ("delegate", false) => include_str!("assets/delegate_usage.txt"),
        ("run", true) => include_str!("assets/run_help.txt"),
        ("run", false) => include_str!("assets/run_usage.txt"),
        ("check", true) => include_str!("assets/check_help.txt"),
        ("check", false) => include_str!("assets/check_usage.txt"),
        ("list", true) => include_str!("assets/list_help.txt"),
        ("list", false) => include_str!("assets/list_usage.txt"),
        (_, true) => include_str!("assets/top_help.txt"),
        (_, false) => include_str!("assets/top_usage.txt"),
    }
}

fn error(action: &str, message: &str) -> i32 {
    out(asset(action, false), true);
    let suffix = if action == "top" {
        String::new()
    } else {
        format!(" {action}")
    };
    out(
        &format!("pseudolife-mcp lease{suffix}: error: {message}\n"),
        true,
    );
    2
}

fn duration_digits(text: &str) -> Result<(&str, u64), String> {
    let trimmed = text.trim_matches(super::whitespace);
    let (digits, factor) = match trimmed.as_bytes().last().map(u8::to_ascii_lowercase) {
        Some(b's') => (&trimmed[..trimmed.len() - 1], 1u64),
        Some(b'm') => (&trimmed[..trimmed.len() - 1], 60),
        Some(b'h') => (&trimmed[..trimmed.len() - 1], 3600),
        Some(b'd') => (&trimmed[..trimmed.len() - 1], 86400),
        _ => (trimmed, 1),
    };
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return Err(format!(
            "not a duration: {} (use 90, 90s, 20m, 2h or 7d)",
            repr(text)
        ));
    }
    Ok((digits, factor))
}

pub fn duration(text: &str, low: u64, high: Option<u64>) -> Result<u64, String> {
    let (digits, factor) = duration_digits(text)?;
    let value = digits
        .parse::<u64>()
        .ok()
        .and_then(|v| v.checked_mul(factor));
    let bounds = || {
        high.map_or_else(
            || format!("at least {low} seconds"),
            |hi| format!("from {low} to {hi} seconds"),
        )
    };
    match value {
        Some(v) if v >= low && high.is_none_or(|hi| v <= hi) => Ok(v),
        _ => Err(format!("{} is out of range: {}", repr(text), bounds())),
    }
}

fn timeout_duration(text: &str) -> Result<f64, String> {
    let (digits, factor) = duration_digits(text)?;
    // Multiply the decimal integer before converting it, as Python does when
    // adding an unbounded duration to its floating-point monotonic clock.
    let mut carry = 0;
    let mut result = Vec::with_capacity(digits.len() + 5);
    for digit in digits.bytes().rev() {
        carry += u64::from(digit - b'0') * factor;
        result.push(b'0' + (carry % 10) as u8);
        carry /= 10;
    }
    while carry > 0 {
        result.push(b'0' + (carry % 10) as u8);
        carry /= 10;
    }
    result.reverse();
    Ok(std::str::from_utf8(&result)
        .expect("decimal digits")
        .parse::<f64>()
        .expect("nonnegative decimal integer"))
}

fn help(action: &str, item: &str) -> Option<i32> {
    let (flag, inline) = item
        .split_once('=')
        .map_or((item, None), |(a, b)| (a, Some(b)));
    let explicit = if flag.starts_with("--") && "--help".starts_with(flag) {
        inline
    } else if let Some(short) = flag.strip_prefix("-h") {
        if !short.bytes().all(|b| b == b'h') {
            Some(short)
        } else {
            inline
        }
    } else {
        return None;
    };
    Some(if let Some(value) = explicit {
        error(
            action,
            &format!(
                "argument -h/--help: ignored explicit argument {}",
                repr(value)
            ),
        )
    } else {
        out(asset(action, true), false);
        0
    })
}

fn valid_name(name: &str) -> bool {
    !name.trim_matches(super::whitespace).is_empty()
        && name.chars().count() <= 120
        && !name.chars().any(char::is_control)
}

// Classify unmatched argv using argparse's literal ASCII-space rule.
fn argument_value(text: &str, options: &[&str]) -> bool {
    if text == "-" || !text.starts_with('-') {
        return true;
    }
    let flag = text.split_once('=').map_or(text, |(flag, _)| flag);
    let known_option = text.starts_with("-h")
        || (flag.starts_with("--") && options.iter().any(|option| option.starts_with(flag)));
    !known_option && (negative_number(text) || text.contains(' '))
}

fn negative_number(text: &str) -> bool {
    let Some(rest) = text.strip_prefix('-') else {
        return false;
    };
    if rest.is_empty() {
        return false;
    }
    if rest.chars().all(|c| decimal_digit(c).is_some()) {
        return true;
    }
    rest.split_once('.').is_some_and(|(a, b)| {
        !b.is_empty()
            && a.chars().all(|c| decimal_digit(c).is_some())
            && b.chars().all(|c| decimal_digit(c).is_some())
    })
}

fn decimal_digit(c: char) -> Option<u32> {
    // Decimal blocks at the pinned CPython 3.11 / Unicode 14 producer.
    const ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16ac0,
        0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    ZEROS
        .iter()
        .find_map(|zero| (c as u32).checked_sub(*zero).filter(|digit| *digit < 10))
}

fn process_id(text: &str) -> Result<u32, String> {
    // int() permits underscores only between digits and one optional sign.
    let invalid = || format!("not a process id: {}", repr(text));
    let trimmed = text.trim_matches(char::is_whitespace);
    let (negative, digits) = if let Some(digits) = trimmed.strip_prefix('-') {
        (true, digits)
    } else {
        (false, trimmed.strip_prefix('+').unwrap_or(trimmed))
    };
    let mut decimal = String::new();
    let mut previous_digit = false;
    for c in digits.chars() {
        if c == '_' && previous_digit {
            previous_digit = false;
            continue;
        }
        let digit = decimal_digit(c).ok_or_else(invalid)?;
        decimal.push(char::from(b'0' + digit as u8));
        previous_digit = true;
    }
    if !previous_digit {
        return Err(invalid());
    }
    if negative || decimal.bytes().all(|digit| digit == b'0') {
        return Err("a process id is a positive whole number".into());
    }
    let pid = decimal.parse::<u32>().map_err(|_| invalid())?;
    #[cfg(unix)]
    if pid > i32::MAX as u32 {
        return Err(invalid());
    }
    Ok(pid)
}

/// The public separator is consumed before parsing, just as lease_cli.main does.
pub fn parse(argv: &[String]) -> Result<Args, i32> {
    let mut renamed;
    let argv = if argv.first().is_some_and(|action| action == "designate") {
        out(
            "pseudolife-mcp lease designate is deprecated: use pseudolife-mcp lease delegate\n",
            true,
        );
        renamed = argv.to_vec();
        renamed[0] = "delegate".into();
        &renamed
    } else {
        argv
    };
    let split = argv.iter().position(|v| v == "--");
    let before = &argv[..split.unwrap_or(argv.len())];
    let mut unrecognized = vec![];
    let mut action_index = 0;
    while let Some(item) = before.get(action_index) {
        if let Some(code) = help("top", item) {
            return Err(code);
        }
        if argument_value(item, &["--help"]) {
            break;
        }
        unrecognized.push(item.as_str());
        action_index += 1;
    }
    let Some(action) = before.get(action_index) else {
        if !unrecognized.is_empty() {
            return Err(error(
                "top",
                &format!("unrecognized arguments: {}", unrecognized.join(" ")),
            ));
        }
        out(asset("top", false), true);
        out(
            "pseudolife-mcp lease: choose run, hold, check, list, break or delegate (--help explains each)\n",
            true,
        );
        return Err(2);
    };
    if !matches!(
        action.as_str(),
        "run" | "hold" | "check" | "list" | "break" | "delegate"
    ) {
        return Err(error(
            "top",
            &format!(
                "argument {{run,hold,check,list,break,delegate}}: invalid choice: {} (choose from 'run', 'hold', 'check', 'list', 'break', 'delegate')",
                repr(action)
            ),
        ));
    }
    let mut args = Args {
        action: action.clone(),
        name: None,
        json: false,
        no_board: false,
        ttl: 120,
        expect: None,
        timeout: None,
        purpose: None,
        while_pid: None,
        worktree: String::new(),
        agent: None,
        hold: 86400,
        command: split.map_or_else(Vec::new, |p| argv[p + 1..].to_vec()),
    };
    let options: &[&str] = if action == "hold" {
        &[
            "--help",
            "--while-pid",
            "--expect",
            "--ttl",
            "--purpose",
            "--worktree",
            "--no-board",
            "--timeout",
        ]
    } else if action == "break" {
        &["--help"]
    } else if action == "delegate" {
        &["--help", "--for"]
    } else if action == "run" {
        &[
            "--help",
            "--expect",
            "--ttl",
            "--purpose",
            "--no-board",
            "--timeout",
        ]
    } else {
        &["--help", "--json"]
    };
    // argparse classifies every token before consuming option values. An
    // ambiguous abbreviation therefore precedes a missing-value diagnostic.
    for item in &before[action_index + 1..] {
        let flag = item.split_once('=').map_or(item.as_str(), |(flag, _)| flag);
        if flag.starts_with("--") {
            let matches: Vec<_> = options
                .iter()
                .copied()
                .filter(|option| option.starts_with(flag))
                .collect();
            if matches.len() > 1 {
                return Err(error(
                    action,
                    &format!(
                        "ambiguous option: {item} could match {}",
                        matches.join(", ")
                    ),
                ));
            }
        }
    }
    let mut index = action_index + 1;
    while index < before.len() {
        let item = &before[index];
        index += 1;
        if let Some(code) = help(action, item) {
            return Err(code);
        }
        let (flag, inline) = item
            .split_once('=')
            .map_or((item.as_str(), None), |(a, b)| (a, Some(b)));
        let matches: Vec<_> = if flag == "-h" {
            vec!["--help"]
        } else if flag.starts_with("--") {
            options
                .iter()
                .copied()
                .filter(|v| v.starts_with(flag))
                .collect()
        } else {
            vec![]
        };
        if matches.len() > 1 {
            return Err(error(
                action,
                &format!(
                    "ambiguous option: {item} could match {}",
                    matches.join(", ")
                ),
            ));
        }
        if let Some(option) = matches.first().copied() {
            if matches!(option, "--help" | "--json" | "--no-board") {
                if let Some(v) = inline {
                    return Err(error(
                        action,
                        &format!("argument {option}: ignored explicit argument {}", repr(v)),
                    ));
                }
                match option {
                    "--help" => {
                        out(asset(action, true), false);
                        return Err(0);
                    }
                    "--json" => args.json = true,
                    _ => args.no_board = true,
                }
                continue;
            }
            let value = if let Some(v) = inline {
                v
            } else {
                let Some(v) = before.get(index).filter(|v| argument_value(v, options)) else {
                    return Err(error(
                        action,
                        &format!("argument {option}: expected one argument"),
                    ));
                };
                index += 1;
                v
            };
            if option == "--while-pid" {
                let pid = process_id(value).map_err(|message| {
                    error(action, &format!("argument --while-pid: {message}"))
                })?;
                args.while_pid = Some(pid);
            } else if option == "--worktree" {
                args.worktree = value.into();
            } else if option == "--purpose" {
                if value.chars().count() > 240 || value.chars().any(char::is_control) {
                    return Err(error(
                        action,
                        "argument --purpose: a purpose is at most 240 characters, without control characters",
                    ));
                }
                args.purpose = Some(value.to_owned());
            } else if option == "--timeout" {
                args.timeout = Some(
                    timeout_duration(value)
                        .map_err(|e| error(action, &format!("argument --timeout: {e}")))?,
                );
            } else {
                let limits = match option {
                    "--ttl" => (30, Some(86400)),
                    "--expect" => (1, Some(604800)),
                    "--for" => (60, Some(604800)),
                    _ => (0, None),
                };
                let value = duration(value, limits.0, limits.1)
                    .map_err(|e| error(action, &format!("argument {option}: {e}")))?;
                match option {
                    "--ttl" => args.ttl = value,
                    "--expect" => args.expect = Some(value),
                    "--for" => args.hold = value,
                    _ => unreachable!("bounded duration option"),
                }
            }
        } else if action == "delegate"
            && args.name.is_some()
            && args.agent.is_none()
            && argument_value(item, options)
        {
            args.agent = Some(item.clone());
        } else if !argument_value(item, options) || args.name.is_some() {
            unrecognized.push(item.as_str());
        } else {
            if !valid_name(item) {
                return Err(error(
                    action,
                    &format!(
                        "argument {}: a lease name is 1 to 120 characters, not blank, without control characters",
                        if action == "delegate" {
                            "PROJECT"
                        } else {
                            "NAME"
                        }
                    ),
                ));
            }
            args.name = Some(item.clone());
        }
    }
    let mut missing = Vec::new();
    if args.name.is_none() && action != "list" {
        missing.push(if action == "delegate" {
            "PROJECT"
        } else {
            "NAME"
        });
    }
    if action == "hold" && args.while_pid.is_none() {
        missing.push("--while-pid");
    }
    if action == "delegate" && args.agent.is_none() {
        missing.push("AGENT");
    }
    if !missing.is_empty() {
        return Err(error(
            action,
            &format!(
                "the following arguments are required: {}",
                missing.join(", ")
            ),
        ));
    }
    if !unrecognized.is_empty() {
        return Err(error(
            "top",
            &format!("unrecognized arguments: {}", unrecognized.join(" ")),
        ));
    }
    if action == "run" && args.command.is_empty() {
        return Err(error(
            "run",
            "put the command to run after --, as in: pseudolife-mcp lease run gpu -- python train.py",
        ));
    }
    if action != "run" && split.is_some() {
        return Err(error(action, &format!("{action} takes no command")));
    }
    Ok(args)
}

#[cfg(test)]
mod argv_tests {
    use super::parse;

    fn parsed(argv: &[&str]) -> super::Args {
        parse(
            &argv
                .iter()
                .map(|item| (*item).to_owned())
                .collect::<Vec<_>>(),
        )
        .unwrap()
    }

    #[test]
    fn argparse_lone_dash_and_ascii_space_are_argument_values() {
        for name in ["-", "- with space", "-1", "-.5", "-١٢"] {
            assert_eq!(
                parsed(&["run", name, "--no-board", "--", "child"])
                    .name
                    .as_deref(),
                Some(name)
            );
        }
        for value in ["-", "-unknown with space"] {
            assert_eq!(
                parsed(&["run", "resource", "--purpose", value, "--", "child"])
                    .purpose
                    .as_deref(),
                Some(value)
            );
            assert_eq!(
                parsed(&["hold", "resource", "--while-pid", "1", "--worktree", value]).worktree,
                value
            );
            assert_eq!(
                parsed(&["delegate", "project", value]).agent.as_deref(),
                Some(value)
            );
            assert_eq!(
                parsed(&["delegate", value, "agent"]).name.as_deref(),
                Some(value)
            );
        }
        assert_eq!(parsed(&["designate", "project", "-"]).action, "delegate");
    }

    #[test]
    fn option_precedence_and_name_validation_remain_unchanged() {
        for argv in [
            vec!["-"],
            vec!["- with space"],
            vec!["hold", "resource", "--while-pid", "-"],
            vec!["hold", "resource", "--while-pid", "- 1"],
            vec!["run", "resource", "--purpose", "--ttl=30 with space"],
            vec!["run", "resource", "--purpose", "--tt=30 with space"],
            vec!["run", "resource", "--purpose", "--t=30 with space"],
            vec!["run", "resource", "--purpose", "-h attached"],
            vec!["run", "resource", "--purpose", "-unknown\tvalue"],
            vec!["run", "- with\tspace", "--", "child"],
            vec!["check", "-unknown"],
        ] {
            assert_eq!(
                parse(
                    &argv
                        .iter()
                        .map(|item| (*item).to_owned())
                        .collect::<Vec<_>>()
                )
                .unwrap_err(),
                2
            );
        }
        assert_eq!(
            parsed(&["run", "resource", "--tt=30", "--", "child"]).ttl,
            30
        );
    }
}

#[cfg(test)]
mod pid_tests {
    use super::process_id;
    #[test]
    fn ordinary_pid_int_grammar_matches_the_original_producer() {
        for (text, expected) in [
            ("1_000", 1000),
            ("١٢٣", 123),
            ("１２３", 123),
            ("+1_000", 1000),
            (" 42\t", 42),
            ("\u{2003}42\u{2003}", 42),
        ] {
            assert_eq!(process_id(text).unwrap(), expected);
        }
        for text in ["+-5", "--5", "_5", "5_", "5__0", "\u{1c}5", "²", "+", ""] {
            assert!(
                process_id(text)
                    .unwrap_err()
                    .starts_with("not a process id:")
            );
        }
        for text in ["-5", "-١٢", "0", "+0_0"] {
            assert_eq!(
                process_id(text).unwrap_err(),
                "a process id is a positive whole number"
            );
        }
    }
    #[test]
    fn pid_range_refuses_without_wrapping() {
        assert_eq!(process_id("2147483647").unwrap(), i32::MAX as u32);
        #[cfg(windows)]
        assert_eq!(process_id("4294967295").unwrap(), u32::MAX);
        #[cfg(unix)]
        for text in ["2147483648", "4294967295"] {
            assert!(
                process_id(text)
                    .unwrap_err()
                    .starts_with("not a process id:")
            );
        }
        assert!(
            process_id("4294967296")
                .unwrap_err()
                .starts_with("not a process id:")
        );
    }
}
