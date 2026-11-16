//! Connect's arguments, without echoing credential-bearing input in errors.
use super::Refusal;
use std::ffi::OsString;

pub(super) const HELP: &str = "Usage: pseudolife-mcp connect [OPTIONS] URL

Re-point existing client registrations at an http(s) daemon origin.
Options may appear before or after URL; values also accept --option=value.

  --client NAMES     all, or comma-separated claude-code,codex,claude-desktop,gemini
  --token-file PATH  Use an existing private token file
  --read-token       Read a token into a new --token-file after confirmation
  --code CODE        Redeem a pairing code
  --read-code        Read the pairing code from the terminal or stdin
  --dry-run          Show the plan without writing files
  --yes              Apply without a confirmation prompt
  --json             Emit a JSON report
  -h, --help         Show this help

connect never creates client registrations. Use --yes for unattended apply.";

#[derive(Debug, Default)]
pub(super) struct Args {
    pub help: bool,
    pub url: String,
    pub token_file: Option<String>,
    pub read_token: bool,
    /// `--code`: a pairing code from `pseudolife-mcp invite`.
    pub code: Option<String>,
    /// `--read-code`: the pairing code, read from stdin.
    pub read_code: bool,
    pub client: Option<String>,
    pub dry_run: bool,
    pub yes: bool,
    pub json: bool,
}

pub(super) fn parse(values: &[OsString]) -> Result<Args, Refusal> {
    let values = values
        .iter()
        .map(|value| {
            value.to_str().ok_or_else(|| {
                Refusal(
                    "an argument is not Unicode; pass Unicode arguments (nothing was changed)"
                        .into(),
                )
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut args = Args::default();
    let mut seen = Vec::new();
    let mut index = 0;
    let mut positional = false;
    while let Some(&word) = values.get(index) {
        index += 1;
        if !positional && matches!(word, "--help" | "-h") {
            args.help = true;
            return Ok(args);
        }
        if !positional && word == "--" {
            positional = true;
            continue;
        }
        if positional || !word.starts_with('-') {
            if word.is_empty() {
                return Err(Refusal(
                    "the daemon URL must not be empty; pass an http(s) daemon origin".into(),
                ));
            }
            if !args.url.is_empty() {
                return Err(Refusal(
                    "give exactly one daemon URL; see `pseudolife-mcp connect --help`".into(),
                ));
            }
            args.url = word.to_owned();
            continue;
        }
        let (option, inline) = word
            .split_once('=')
            .map_or((word, None), |(key, value)| (key, Some(value)));
        match option {
            "--token-file" | "--client" | "--code" => {
                if seen.contains(&option) {
                    return Err(Refusal(format!(
                        "{option} was given more than once; pass it once"
                    )));
                }
                seen.push(option);
                let value = inline
                    .or_else(|| {
                        let value = values.get(index).copied()?;
                        if value.starts_with("--") || value == "-h" {
                            return None;
                        }
                        index += 1;
                        Some(value)
                    })
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| {
                        Refusal(format!("{option} needs a value; use {option}=VALUE"))
                    })?;
                match option {
                    "--token-file" => args.token_file = Some(value.into()),
                    "--client" => args.client = Some(value.into()),
                    _ => args.code = Some(value.into()),
                }
            }
            "--read-token" | "--read-code" | "--dry-run" | "--yes" | "--json" => {
                if inline.is_some() {
                    return Err(Refusal(format!(
                        "{option} takes no value; use {option} alone"
                    )));
                }
                match option {
                    "--read-token" => args.read_token = true,
                    "--read-code" => args.read_code = true,
                    "--dry-run" => args.dry_run = true,
                    "--yes" => args.yes = true,
                    _ => args.json = true,
                }
            }
            _ => {
                // An arbitrary dash-leading word may be a pasted token. Only
                // suggest known option names, never echo the supplied bytes.
                let hint = [
                    "--token-file",
                    "--read-token",
                    "--read-code",
                    "--dry-run",
                    "--client",
                    "--code",
                    "--json",
                    "--yes",
                ]
                .into_iter()
                .find(|known| option.len() > 2 && known.starts_with(option));
                let remedy = hint.map_or_else(
                    || "see `pseudolife-mcp connect --help`".to_owned(),
                    |known| format!("use {known}; see `pseudolife-mcp connect --help`"),
                );
                return Err(Refusal(format!("unknown option; {remedy}")));
            }
        }
    }
    if args.url.is_empty() {
        return Err(Refusal(
            "a daemon URL is required; use `pseudolife-mcp connect [OPTIONS] URL`".into(),
        ));
    }
    Ok(args)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<OsString> {
        items.iter().map(OsString::from).collect()
    }

    #[test]
    fn producer_shapes_parse() {
        let args = parse(&argv(&[
            "http://h:1",
            "--token-file",
            "t",
            "--client",
            "claude-code,codex",
            "--dry-run",
            "--json",
        ]))
        .unwrap();
        assert_eq!(args.token_file.as_deref(), Some("t"));
        assert!(args.dry_run && args.json && !args.yes);
        assert!(parse(&argv(&["http://h:1"])).is_ok());
        assert!(parse(&argv(&["http://h:1", "--yes", "--json"])).is_ok());
        let paired = parse(&argv(&["http://h:1", "--code", "AAAA-BBBB-CCCC", "--yes"])).unwrap();
        assert_eq!(paired.code.as_deref(), Some("AAAA-BBBB-CCCC"));
        assert!(
            parse(&argv(&["http://h:1", "--read-code"]))
                .unwrap()
                .read_code
        );
    }

    #[test]
    fn invalid_shapes_have_usage_errors() {
        for shape in [
            &["http://h:1", "--token"][..],
            &["http://h:1", "--code"],
            &["http://h:1", "--code", "--yes"],
            &["http://h:1", "--client"],
            &["http://h:1", "--client", ""],
            &[],
        ] {
            assert!(parse(&argv(shape)).is_err(), "{shape:?}");
        }
    }
}
