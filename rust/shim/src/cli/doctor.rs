//! The explicit disposable-database refusal, before any doctor diagnostics.
use std::{
    ffi::OsString,
    io::{self, Write},
    process::ExitCode,
};

const OPTIONS: [&str; 5] = [
    "--help",
    "--timeout",
    "--host",
    "--agent-state",
    "--disposable-proof",
];
const HOSTS: [&str; 4] = ["codex", "claude-code", "claude-desktop", "generic"];
const USAGE: &str = "usage: pseudolife-stdio doctor [-h] [--timeout TIMEOUT]\n       [--host {codex,claude-code,claude-desktop,generic}]\n       [--agent-state AGENT_STATE] [--disposable-proof]\n";
const REFUSAL: &str = "{\"ok\": false, \"error\": \"ExplicitDisposableDatabaseRequired\", \"recovery\": \"Set PSEUDOLIFE_TEST_DATABASE_URL to an explicitly disposable fixture server; no configured bank or bench default is used.\"}\n";

struct Args {
    timeout: f64,
    proof: bool,
    agent_state: bool,
}

fn option(raw: &str) -> Result<Option<usize>, String> {
    let name = raw.split_once('=').map_or(raw, |(name, _)| name);
    if name == "-h" || name.starts_with("-h") && !name.starts_with("--") {
        return Ok(Some(0));
    }
    let matching: Vec<_> = OPTIONS
        .iter()
        .enumerate()
        .filter(|(_, option)| name.starts_with("--") && option.starts_with(name))
        .map(|(index, _)| index)
        .collect();
    if matching.len() > 1 {
        return Err(format!(
            "ambiguous option: {raw} could match {}",
            matching
                .iter()
                .map(|index| OPTIONS[*index])
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Ok(matching.first().copied())
}

fn optional(raw: &str) -> bool {
    let negative = raw.strip_prefix('-').is_some_and(|number| {
        let number = number.strip_suffix('\n').unwrap_or(number);
        !number.is_empty() && number.bytes().all(|b| b.is_ascii_digit())
            || number.split_once('.').is_some_and(|(left, right)| {
                left.bytes().all(|b| b.is_ascii_digit())
                    && !right.is_empty()
                    && right.bytes().all(|b| b.is_ascii_digit())
            })
    });
    raw.starts_with('-') && raw != "-" && !raw.contains(' ') && !negative
}

fn timeout(raw: &str) -> Option<f64> {
    // Doctor's <= 0 check deliberately permits NaN and positive infinity.
    let raw = raw.trim_matches([' ', '\t', '\n', '\r', '\x0b', '\x0c']);
    let bytes = raw.as_bytes();
    if bytes.iter().enumerate().any(|(index, byte)| {
        *byte == b'_'
            && (index == 0
                || index + 1 == bytes.len()
                || !bytes[index - 1].is_ascii_digit()
                || !bytes[index + 1].is_ascii_digit())
    }) {
        return None;
    }
    let raw = raw.replace('_', "").to_ascii_lowercase();
    match raw.as_str() {
        "nan" | "+nan" | "-nan" => Some(f64::NAN),
        "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
        "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
        _ if raw
            .bytes()
            .all(|b| b.is_ascii_digit() || b".e+-".contains(&b)) =>
        {
            raw.parse().ok()
        }
        _ => None,
    }
}

fn parse(values: &[String]) -> Result<Option<Args>, String> {
    // argparse identifies ambiguous options before executing help actions.
    for raw in values.iter().take_while(|raw| raw.as_str() != "--") {
        option(raw)?;
    }
    let mut args = Args {
        timeout: 20.0,
        proof: false,
        agent_state: false,
    };
    let mut extras = Vec::new();
    let mut index = 0;
    while index < values.len() {
        let raw = &values[index];
        if raw == "--" {
            extras.extend(values[index..].iter().cloned());
            break;
        }
        let Some(selected) = option(raw)? else {
            extras.push(raw.clone());
            index += 1;
            continue;
        };
        let explicit = raw.split_once('=').map(|(_, value)| value);
        let label = if selected == 0 {
            "-h/--help"
        } else {
            OPTIONS[selected]
        };
        if selected == 0 || selected == 4 {
            // A short help cluster executes help before its remaining letters.
            let short_help_cluster =
                selected == 0 && raw.starts_with("-h") && !raw.starts_with("-h=") && raw.len() > 2;
            if let Some(value) = explicit.filter(|_| !short_help_cluster) {
                return Err(format!(
                    "argument {label}: ignored explicit argument {}",
                    super::mode_repr(value)
                ));
            }
            if selected == 0 {
                // Complete original help remains deferred with normal diagnostics.
                return Ok(None);
            }
            args.proof = true;
        } else {
            let value = if let Some(value) = explicit {
                value
            } else {
                index += 1;
                let Some(value) = values
                    .get(index)
                    .filter(|value| !optional(value) && value.as_str() != "--")
                else {
                    return Err(format!("argument {label}: expected one argument"));
                };
                value
            };
            match selected {
                1 => {
                    args.timeout = timeout(value).ok_or_else(|| {
                        format!(
                            "argument --timeout: invalid float value: {}",
                            super::mode_repr(value)
                        )
                    })?;
                }
                2 if !HOSTS.contains(&value) => {
                    return Err(format!(
                        "argument --host: invalid choice: {} (choose from 'codex', 'claude-code', 'claude-desktop', 'generic')",
                        super::mode_repr(value)
                    ));
                }
                3 => args.agent_state = true,
                _ => {}
            }
        }
        index += 1;
    }
    if !extras.is_empty() {
        return Err(format!("unrecognized arguments: {}", extras.join(" ")));
    }
    Ok(Some(args))
}

fn error(message: &str) -> ExitCode {
    let text = format!("{USAGE}pseudolife-stdio doctor: error: {message}\n");
    if io::stderr()
        .lock()
        .write_all(&super::text_bytes(&text))
        .is_ok()
    {
        ExitCode::from(2)
    } else {
        ExitCode::FAILURE
    }
}

pub(super) fn run(values: Vec<OsString>) -> Option<ExitCode> {
    // Non-ASCII argv (including Unicode float spellings) is outside this leaf.
    // Defer before classification rather than reinterpret an OS string lossily.
    let values: Option<Vec<_>> = values
        .iter()
        .map(|value| {
            value
                .to_str()
                .filter(|raw| raw.is_ascii())
                .map(str::to_owned)
        })
        .collect();
    let args = match parse(&values?) {
        Ok(args) => args?,
        Err(message) => return Some(error(&message)),
    };
    if args.timeout <= 0.0 {
        return Some(error("--timeout must be positive"));
    }
    if !args.proof {
        return None;
    }
    if args.agent_state {
        return Some(error("--disposable-proof cannot use a saved --agent-state"));
    }
    if std::env::var_os("PSEUDOLIFE_TEST_DATABASE_URL").is_some_and(|dsn| !dsn.is_empty()) {
        return None;
    }
    Some(
        if io::stdout()
            .lock()
            .write_all(&super::text_bytes(REFUSAL))
            .is_ok()
        {
            ExitCode::from(2)
        } else {
            ExitCode::FAILURE
        },
    )
}
