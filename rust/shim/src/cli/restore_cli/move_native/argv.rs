//! The argv the native `move` answers, and its refusals before any effect.
//!
//! `move_cli.rs` decides the oracle's refusals first; every argv it passes
//! on comes here. This parser takes only the canonical spellings (each
//! option by its full name, a value as the next argument not starting with
//! `-`, a flag at most once); any other shape returns `None` and keeps the
//! dispatcher's deferral (the edge-shape sweep is separate work).
use std::ffi::OsString;

/// What `move` was asked.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct Request {
    pub to: String,
    pub target_url: Option<String>,
    pub target_checkout: Option<String>,
    pub target_work_dir: Option<String>,
    pub target_env_file: Option<String>,
    pub target_compose_files: Vec<String>,
    pub no_keep_tokens: bool,
    pub resume: bool,
    pub dry_run: bool,
    pub yes: bool,
    pub json: bool,
    pub expose_target: bool,
    pub target_release: bool,
}

/// The canonical argv, or `None` (the dispatcher defers it).
pub(super) fn parse(arguments: &[OsString]) -> Option<Request> {
    let words: Vec<&str> = arguments
        .iter()
        .map(|a| a.to_str())
        .collect::<Option<_>>()?;
    let mut request = Request::default();
    let mut to = None;
    let mut index = 0;
    while index < words.len() {
        let word = words[index];
        let mut value = || -> Option<String> {
            index += 1;
            let value = *words.get(index)?;
            (!value.starts_with('-')).then(|| value.to_owned())
        };
        let flag = |seen: &mut bool| -> Option<()> {
            if *seen {
                return None;
            }
            *seen = true;
            Some(())
        };
        match word {
            "--to" if to.is_none() => to = Some(value()?),
            "--target-url" if request.target_url.is_none() => request.target_url = Some(value()?),
            "--target-checkout" if request.target_checkout.is_none() => {
                request.target_checkout = Some(value()?)
            }
            "--target-work-dir" if request.target_work_dir.is_none() => {
                request.target_work_dir = Some(value()?)
            }
            "--target-env-file" if request.target_env_file.is_none() => {
                request.target_env_file = Some(value()?)
            }
            "--target-compose-file" => request.target_compose_files.push(value()?),
            "--no-keep-tokens" => flag(&mut request.no_keep_tokens)?,
            "--resume" => flag(&mut request.resume)?,
            "--dry-run" => flag(&mut request.dry_run)?,
            "--yes" => flag(&mut request.yes)?,
            "--json" => flag(&mut request.json)?,
            "--expose-target" => flag(&mut request.expose_target)?,
            "--target-release" => flag(&mut request.target_release)?,
            _ => return None,
        }
        index += 1;
    }
    request.to = to?;
    Some(request)
}

const NOTHING: &str = "Nothing was changed";

/// The first refusal the request meets before any effect (exit 2). The
/// first two are the argv checks Python's move makes, in its order and its
/// words (as `move_cli` prints them); the rest are this client's.
pub(super) fn refusal(request: &Request) -> Option<String> {
    if let Some(url) = &request.target_url
        && let Err(reason) = origin(url)
    {
        return Some(format!("--target-url {reason}"));
    }
    let to = &request.to;
    if to.is_empty()
        || to.starts_with('-')
        || to.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Some(
            "--to takes an ssh target such as root@box or a ~/.ssh/config alias".to_owned(),
        );
    }
    let text = if !request.dry_run {
        "the native client plans a move but does not perform one yet: run it with --dry-run to check both hosts and print the plan; the move itself needs the Python client's move for now (pseudolife-mcp 0.23.x installed with pip or pipx)"
            .to_owned()
    } else if request.target_checkout.is_some() {
        "--target-checkout is not supported: move goes to an installed (release) Docker bank on the target, with the native pseudolife-mcp client there; drop --target-checkout"
            .to_owned()
    } else if request.expose_target {
        "--expose-target is not available in the native client yet: expose the target's daemon yourself (for example with Tailscale serve) and name the URL clients will use with --target-url <url>"
            .to_owned()
    } else if request.resume {
        "--resume arrives with the native move itself; a dry run has nothing to resume, so drop --resume"
            .to_owned()
    } else if request.target_env_file.is_some() || !request.target_compose_files.is_empty() {
        "--target-env-file and --target-compose-file arrive with the native move itself; a dry run reads the target's containers as they are, so drop them"
            .to_owned()
    } else if request.target_url.is_none() {
        "--target-url is required: name the URL clients will use to reach the target's daemon, such as http://100.64.0.2:8765"
            .to_owned()
    } else if !request.no_keep_tokens {
        "carrying the source's tokens to the target is not available in the native client yet: pass --no-keep-tokens (the target keeps its own tokens, and clients re-pair or use the target's)"
            .to_owned()
    } else if let Some(dir) = &request.target_work_dir
        && dir.chars().any(char::is_control)
    {
        "--target-work-dir holds a control character".to_owned()
    } else if let Some(dir) = &request.target_work_dir
        && !(dir.starts_with('/') || dir.starts_with("~/"))
    {
        "--target-work-dir must be an absolute path or start with ~/ on the target".to_owned()
    } else if let Some(dir) = &request.target_work_dir
        && dir.split('/').any(|part| part == "..")
    {
        "--target-work-dir must name the directory itself, without a .. component".to_owned()
    } else {
        return None;
    };
    Some(format!("{text}. {NOTHING}"))
}

/// An `http(s)://host[:port][/]` origin, its host bracketed when IPv6.
pub(super) fn origin(url: &str) -> Result<(), &'static str> {
    const SHAPE: &str =
        "must be an http(s) origin such as http://100.64.0.2:8765 (no path, query or credentials)";
    let lower = url.to_ascii_lowercase();
    let rest = lower
        .strip_prefix("http://")
        .or_else(|| lower.strip_prefix("https://"))
        .ok_or(SHAPE)?;
    let rest = rest.strip_suffix('/').unwrap_or(rest);
    if rest.is_empty()
        || rest.contains(['/', '?', '#', '@', '\\'])
        || rest.chars().any(|c| c.is_whitespace() || c.is_control())
    {
        return Err(SHAPE);
    }
    let (host, port) = if let Some(inner) = rest.strip_prefix('[') {
        let (address, after) = inner.split_once(']').ok_or(SHAPE)?;
        if address.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(SHAPE);
        }
        match after {
            "" => (address, None),
            other => (address, Some(other.strip_prefix(':').ok_or(SHAPE)?)),
        }
    } else {
        match rest.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (rest, None),
        }
    };
    if host.is_empty()
        || !host
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-:".contains(&b))
    {
        return Err(SHAPE);
    }
    if let Some(port) = port
        && !(!port.is_empty()
            && port.len() <= 5
            && port.bytes().all(|b| b.is_ascii_digit())
            && port.parse::<u32>().is_ok_and(|p| (1..=65535).contains(&p)))
    {
        return Err(SHAPE);
    }
    Ok(())
}

/// `url` without a trailing slash, for joining `/health`.
pub(super) fn base(url: &str) -> &str {
    url.strip_suffix('/').unwrap_or(url)
}
