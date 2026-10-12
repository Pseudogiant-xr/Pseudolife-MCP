//! Canonical argv only: the target URL first, then each option at most once
//! with its value as the next argument. Pairing (`--code`, `--read-code`),
//! help, abbreviations, `--opt=value`, `--` and values that start with `-`
//! defer, as does anything that is not valid Unicode.
use std::ffi::OsString;

#[derive(Debug, Default)]
pub(super) struct Args {
    pub url: String,
    pub token_file: Option<String>,
    pub read_token: bool,
    pub client: Option<String>,
    pub dry_run: bool,
    pub yes: bool,
    pub json: bool,
}

pub(super) fn parse(values: &[OsString]) -> Option<Args> {
    let mut values = values.iter().map(|value| value.to_str());
    let url = values.next()??;
    if url.is_empty() || url.starts_with('-') {
        return None;
    }
    let mut args = Args {
        url: url.to_owned(),
        ..Args::default()
    };
    let mut seen = Vec::new();
    while let Some(option) = values.next() {
        let option = option?;
        if seen.contains(&option) {
            return None;
        }
        seen.push(option);
        let mut value = || -> Option<String> {
            let value = values.next()??;
            (!value.is_empty() && !value.starts_with('-')).then(|| value.to_owned())
        };
        match option {
            "--token-file" => args.token_file = Some(value()?),
            "--client" => args.client = Some(value()?),
            "--read-token" => args.read_token = true,
            "--dry-run" => args.dry_run = true,
            "--yes" => args.yes = true,
            "--json" => args.json = true,
            _ => return None,
        }
    }
    Some(args)
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
        assert!(parse(&argv(&["http://h:1"])).is_some());
        assert!(parse(&argv(&["http://h:1", "--yes", "--json"])).is_some());
    }

    #[test]
    fn other_shapes_defer() {
        for shape in [
            &["--dry-run", "http://h:1"][..],
            &["http://h:1", "--yes", "--yes"],
            &["http://h:1", "--token-file=t"],
            &["http://h:1", "--token"],
            &["http://h:1", "--code", "AAAA-BBBB-CCCC"],
            &["http://h:1", "--read-code"],
            &["http://h:1", "--token-file", "-t"],
            &["http://h:1", "--client"],
            &["http://h:1", "--client", ""],
            &["http://h:1", "-h"],
            &["http://h:1", "--"],
            &[],
        ] {
            assert!(parse(&argv(shape)).is_none(), "{shape:?}");
        }
    }
}
