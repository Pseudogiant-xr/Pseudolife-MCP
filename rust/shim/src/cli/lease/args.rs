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
}

fn asset(action: &str, help: bool) -> &'static str {
    match (action, help) {
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

fn negative_number(text: &str) -> bool {
    let Some(rest) = text.strip_prefix('-') else {
        return false;
    };
    if rest.is_empty() {
        return false;
    }
    if rest.bytes().all(|b| b.is_ascii_digit()) {
        return true;
    }
    rest.split_once('.').is_some_and(|(a, b)| {
        !b.is_empty()
            && a.bytes().all(|c| c.is_ascii_digit())
            && b.bytes().all(|c| c.is_ascii_digit())
    })
}

/// The public separator is consumed before parsing, just as lease_cli.main does.
pub fn parse(argv: &[String]) -> Result<Args, i32> {
    let split = argv.iter().position(|v| v == "--");
    let before = &argv[..split.unwrap_or(argv.len())];
    let mut unrecognized = vec![];
    let mut action_index = 0;
    while let Some(item) = before.get(action_index) {
        if let Some(code) = help("top", item) {
            return Err(code);
        }
        if !item.starts_with('-') || negative_number(item) {
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
    if matches!(action.as_str(), "hold" | "break" | "delegate" | "designate") {
        out(
            &format!(
                "pseudolife-stdio: lease action {} is deferred in this candidate\n",
                repr(action)
            ),
            true,
        );
        return Err(1);
    }
    if !matches!(action.as_str(), "run" | "check" | "list") {
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
        command: split.map_or_else(Vec::new, |p| argv[p + 1..].to_vec()),
    };
    let options: &[&str] = if action == "run" {
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
                let Some(v) = before
                    .get(index)
                    .filter(|v| !v.starts_with('-') || negative_number(v))
                else {
                    return Err(error(
                        action,
                        &format!("argument {option}: expected one argument"),
                    ));
                };
                index += 1;
                v
            };
            if option == "--purpose" {
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
                    _ => (0, None),
                };
                let value = duration(value, limits.0, limits.1)
                    .map_err(|e| error(action, &format!("argument {option}: {e}")))?;
                match option {
                    "--ttl" => args.ttl = value,
                    "--expect" => args.expect = Some(value),
                    _ => unreachable!("bounded duration option"),
                }
            }
        } else if item.starts_with('-') && !negative_number(item) || args.name.is_some() {
            unrecognized.push(item.as_str());
        } else {
            if !valid_name(item) {
                return Err(error(
                    action,
                    "argument NAME: a lease name is 1 to 120 characters, not blank, without control characters",
                ));
            }
            args.name = Some(item.clone());
        }
    }
    if args.name.is_none() && action != "list" {
        return Err(error(action, "the following arguments are required: NAME"));
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
