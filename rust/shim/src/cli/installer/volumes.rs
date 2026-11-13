//! `pseudolife-mcp installer volume-names --repo R --env-file F` (formerly
//! `ops/preflight.py --volume-names`): the bank and state volume names as
//! Docker Compose resolves them from ops/.env (or ops/.env.example when it
//! does not exist yet), one per line, defaults filled in and validated.
use std::{
    io::{self, Write},
    path::Path,
};

// ops/preflight.py printed every refusal as "FAIL <message>".
const UNREACHED: &str =
    "Cannot inspect Docker safely. Run docker info, fix Docker access, then rerun.";
const RESOLVE_FAILED: &str = "Cannot resolve ops/.env with Docker Compose. Run docker compose --env-file ops/.env -f ops/docker-compose.yml config --quiet, fix the reported configuration, then rerun. Nothing changed.";
const INVALID: &str = "Invalid bank/state volume name. Set literal Docker volume names in ops/.env, then rerun. Nothing changed.";

fn valid(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && name.len() >= 2
        && chars.all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c))
}

/// The two names from `config --environment` output, defaults filled in.
fn names(output: &str) -> Result<(String, String), &'static str> {
    let value = |key: &str| {
        output
            .lines()
            .filter_map(|line| line.split_once('='))
            .filter(|(k, _)| *k == key)
            .map(|(_, v)| v.to_owned())
            .next_back()
            .filter(|v| !v.is_empty())
    };
    let bank = value("PSEUDOLIFE_BANK_VOLUME").unwrap_or_else(|| "pseudolife-mcp-bank".to_owned());
    let state =
        value("PSEUDOLIFE_STATE_VOLUME").unwrap_or_else(|| "pseudolife-mcp-state".to_owned());
    if !valid(&bank) || !valid(&state) {
        return Err(INVALID);
    }
    Ok((bank, state))
}

pub(super) fn run(args: &[String]) -> u8 {
    let (mut repo, mut env_file) = (None, None);
    let mut items = args.iter();
    while let Some(item) = items.next() {
        match (item.as_str(), items.next()) {
            ("--repo", Some(value)) => repo = Some(value.clone()),
            ("--env-file", Some(value)) => env_file = Some(value.clone()),
            _ => {
                let _ = writeln!(
                    io::stderr().lock(),
                    "installer volume-names: usage: --repo R --env-file F"
                );
                return 2;
            }
        }
    }
    let (Some(repo), Some(env_file)) = (repo, env_file) else {
        let _ = writeln!(
            io::stderr().lock(),
            "installer volume-names: usage: --repo R --env-file F"
        );
        return 2;
    };
    let ops = Path::new(&repo).join("ops");
    let source = if Path::new(&env_file).is_file() {
        Path::new(&env_file).to_path_buf()
    } else {
        ops.join(".env.example")
    };
    // Compose owns dotenv syntax, interpolation and exported-value precedence;
    // its output stays private, only the two names leave this process. A
    // wedged Docker is given up on after 20 seconds, as ops/preflight.py did.
    let argv: Vec<String> = [
        "docker".into(),
        "compose".into(),
        "--env-file".into(),
        source.display().to_string(),
        "-f".into(),
        ops.join("docker-compose.yml").display().to_string(),
        "config".into(),
        "--environment".into(),
    ]
    .to_vec();
    let text = match super::state::output_within(&argv, std::time::Duration::from_secs(20)) {
        Ok(text) => text,
        Err(why) => {
            let message = match why {
                super::state::NotRun::Unreached => UNREACHED,
                super::state::NotRun::Exited => RESOLVE_FAILED,
            };
            let _ = writeln!(io::stderr().lock(), "FAIL {message}");
            return 1;
        }
    };
    match names(&text) {
        Ok((bank, state)) => u8::from(writeln!(io::stdout().lock(), "{bank}\n{state}").is_err()),
        Err(message) => {
            let _ = writeln!(io::stderr().lock(), "FAIL {message}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_take_compose_values_or_the_defaults_and_refuse_odd_ones() {
        assert_eq!(
            names("X=1\n").unwrap(),
            ("pseudolife-mcp-bank".into(), "pseudolife-mcp-state".into())
        );
        assert_eq!(
            names("PSEUDOLIFE_BANK_VOLUME=pseudolife-mcp-bank-pg18\nPSEUDOLIFE_STATE_VOLUME=s.1\n")
                .unwrap(),
            ("pseudolife-mcp-bank-pg18".into(), "s.1".into())
        );
        assert!(names("PSEUDOLIFE_BANK_VOLUME=-bad\n").is_err());
        assert!(names("PSEUDOLIFE_STATE_VOLUME=a b\n").is_err());
    }
}
