//! `pseudolife-mcp tunnel` argv: the commands this client runs, their help,
//! and a refusal naming the cause and the next step for anything else.
use std::ffi::OsString;

use super::{Command, Options, Setup};

/// What the argv asks for: a command to run, or help to print.
pub(super) enum Parsed {
    Run(Options),
    Help(String),
}

const COMMANDS: &str = "setup, status, verify, run, stop or update";

pub(super) const OVERVIEW: &str = "usage: pseudolife-mcp tunnel <command> [options]

Connect a ChatGPT developer app to this daemon through a Secure MCP Tunnel.

Commands:
  setup    save or resume a tunnel profile, import or renew its runtime key
  status   show a profile's setup, key, launch and cloud verification
  verify   print a read-only prompt that proves cloud access
  run      run the tunnel in this terminal until it is stopped
  stop     ask a running tunnel to stop
  update   check saved tunnels after a client update

Every command takes --profile NAME (default dot) and --profile-dir DIR.
Run `pseudolife-mcp tunnel <command> --help` for its options.
";

const COMMON: &str = "  --profile NAME      the saved profile (default dot)
  --profile-dir DIR   the private profile directory (default ~/.pseudolife-mcp/tunnel)
";

fn help(command: Command) -> String {
    let (usage, summary, own) = match command {
        Command::Setup => (
            "setup [options]",
            "Save or resume a tunnel profile, then import or renew its runtime key.",
            "  --daemon-url URL        the daemon to expose (with --token-file)
  --token-file FILE       an owner-only file holding the daemon credential
  --accept-access         consent to this access path
  --tunnel-id ID          the Platform tunnel this profile uses
  --organization-id ID    the Platform organization of that tunnel
  --catalog current|full  the tool discovery tier to keep available
  --key-expires-at TIME   the runtime key's expiry (ISO 8601)
  --read-key              read the runtime key at this terminal, without echo
  --key-file FILE         read the runtime key from an owner-only file
  --restore-key           go back to the key a renewal retained
  --discard-previous-key  drop the key a renewal retained
",
        ),
        Command::Status => (
            "status [options]",
            "Show a profile's setup, key, launch and cloud verification.",
            "  --json              print one JSON object
",
        ),
        Command::Verify => (
            "verify [options]",
            "Print a read-only prompt to paste into ChatGPT that proves cloud access.",
            "",
        ),
        Command::Run => (
            "run [options]",
            "Run the tunnel in this terminal until `tunnel stop` or Ctrl-C ends it.",
            "",
        ),
        Command::Stop => ("stop [options]", "Ask a running tunnel to stop.", ""),
        Command::Update => (
            "update [options]",
            "Check saved tunnels after a client update and refresh running ones.",
            "",
        ),
    };
    format!(
        "usage: pseudolife-mcp tunnel {usage}\n\n{summary}\n\nOptions:\n{own}{COMMON}  -h, --help          show this help\n"
    )
}

impl Command {
    pub(super) fn word(self) -> &'static str {
        match self {
            Command::Status => "status",
            Command::Update => "update",
            Command::Verify => "verify",
            Command::Setup => "setup",
            Command::Run => "run",
            Command::Stop => "stop",
        }
    }
}

/// The refusal for a form of `tunnel` this client does not run yet.
fn not_built(form: &str) -> Option<String> {
    let instead = match form {
        "start" | "setup --start" => {
            "Run `pseudolife-mcp tunnel run` in a terminal you keep open instead; `pseudolife-mcp tunnel stop` ends it"
        }
        "service" => {
            "To keep a tunnel running, run `pseudolife-mcp tunnel run` in a terminal you keep open"
        }
        "doctor" => {
            "`pseudolife-mcp tunnel status` and `pseudolife-mcp doctor` check the saved tunnels"
        }
        _ => return None,
    };
    Some(format!(
        "tunnel {form} is not in the native client yet. {instead}"
    ))
}

const BROWSER: &str = "tunnel setup --open-browser is not in the native client yet. Open the step yourself: tunnels https://platform.openai.com/settings/organization/tunnels, keys https://platform.openai.com/settings/organization/api-keys, app https://chatgpt.com/plugins";

/// `None` for an option that takes no value, else the option's slot.
enum Takes {
    Flag(fn(&mut Options)),
    Value(fn(&mut Options, String) -> Result<(), String>),
}

fn option(command: Command, name: &str) -> Option<Takes> {
    use Takes::{Flag, Value};
    fn setup(options: &mut Options) -> &mut Setup {
        &mut options.setup
    }
    Some(match (name, command) {
        ("--profile", _) => Value(|o, v| {
            o.profile = v;
            Ok(())
        }),
        ("--profile-dir", _) => Value(|o, v| {
            o.profile_dir = Some(v);
            Ok(())
        }),
        ("--json", Command::Status) => Flag(|o| o.json = true),
        ("--accept-access", Command::Setup) => Flag(|o| setup(o).accept_access = true),
        ("--read-key", Command::Setup) => Flag(|o| setup(o).read_key = true),
        ("--restore-key", Command::Setup) => Flag(|o| setup(o).restore_key = true),
        ("--discard-previous-key", Command::Setup) => {
            Flag(|o| setup(o).discard_previous_key = true)
        }
        ("--daemon-url", Command::Setup) => Value(|o, v| {
            setup(o).daemon_url = Some(v);
            Ok(())
        }),
        ("--token-file", Command::Setup) => Value(|o, v| {
            setup(o).token_file = Some(v);
            Ok(())
        }),
        ("--key-file", Command::Setup) => Value(|o, v| {
            setup(o).key_file = Some(v);
            Ok(())
        }),
        ("--tunnel-id", Command::Setup) => Value(|o, v| {
            setup(o).tunnel_id = Some(v);
            Ok(())
        }),
        ("--organization-id", Command::Setup) => Value(|o, v| {
            setup(o).organization_id = Some(v);
            Ok(())
        }),
        ("--key-expires-at", Command::Setup) => Value(|o, v| {
            setup(o).key_expires_at = Some(v);
            Ok(())
        }),
        ("--catalog", Command::Setup) => Value(|o, v| {
            if !matches!(v.as_str(), "current" | "full") {
                return Err(format!(
                    "--catalog takes current or full, not {}",
                    shown(&v)
                ));
            }
            setup(o).catalog = Some(v);
            Ok(())
        }),
        _ => return None,
    })
}

/// An argument as a refusal shows it.
fn shown(text: &str) -> String {
    super::quote_tunnel_arg(text)
}

fn usage_hint(command: &str) -> String {
    format!("see `pseudolife-mcp tunnel {command} --help`")
}

/// The command to run, help to print, or the refusal (exit 2) naming what
/// is wrong with the argv and what to run instead.
pub(super) fn read(values: &[OsString]) -> Result<Parsed, String> {
    let values: Vec<&str> = values
        .iter()
        .map(|value| value.to_str())
        .collect::<Option<_>>()
        .ok_or_else(|| {
            "tunnel arguments must be valid text; retype the command without that argument"
                .to_owned()
        })?;
    let Some((command, rest)) = values.split_first() else {
        return Err(format!(
            "tunnel needs a command: {COMMANDS}; see `pseudolife-mcp tunnel --help`"
        ));
    };
    let command = match *command {
        "-h" | "--help" | "help" => return Ok(Parsed::Help(OVERVIEW.to_owned())),
        "status" => Command::Status,
        "update" => Command::Update,
        "verify" => Command::Verify,
        "setup" => Command::Setup,
        "run" => Command::Run,
        "stop" => Command::Stop,
        other => {
            return Err(not_built(other).unwrap_or_else(|| {
                format!(
                    "unknown tunnel command {}; the commands are {COMMANDS}; see `pseudolife-mcp tunnel --help`",
                    shown(other)
                )
            }));
        }
    };
    let word = command.word();
    let mut options = Options {
        command,
        profile: "dot".into(),
        profile_dir: None,
        json: false,
        setup: Setup::default(),
    };
    let mut seen: Vec<String> = Vec::new();
    let mut rest = rest.iter();
    while let Some(argument) = rest.next() {
        if matches!(*argument, "-h" | "--help") {
            return Ok(Parsed::Help(help(command)));
        }
        let (name, joined) = match argument.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value)),
            _ => (*argument, None),
        };
        if command == Command::Setup {
            match name {
                "--start" => return Err(not_built("setup --start").unwrap_or_default()),
                "--open-browser" => return Err(BROWSER.to_owned()),
                _ => {}
            }
        }
        let Some(takes) = option(command, name) else {
            return Err(if name.starts_with('-') {
                format!(
                    "tunnel {word} does not take {}; {}",
                    shown(name),
                    usage_hint(word)
                )
            } else {
                format!(
                    "tunnel {word} takes no argument {}; {}",
                    shown(name),
                    usage_hint(word)
                )
            });
        };
        if seen.iter().any(|earlier| earlier == name) {
            return Err(format!("{name} is given twice; give it once"));
        }
        seen.push(name.to_owned());
        match takes {
            Takes::Flag(set) => {
                if joined.is_some() {
                    return Err(format!("{name} takes no value; give it as {name}"));
                }
                set(&mut options);
            }
            Takes::Value(set) => {
                let value = match joined {
                    Some(value) => value,
                    None => match rest.next() {
                        Some(value) if !value.starts_with('-') => value,
                        _ => {
                            return Err(format!("{name} needs a value; {}", usage_hint(word)));
                        }
                    },
                };
                set(&mut options, value.to_owned())?;
            }
        }
    }
    Ok(Parsed::Run(options))
}
