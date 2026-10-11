//! ArgumentParser grammar and formatting for backup's three value options.
use std::{
    ffi::OsString,
    path::{Component, Path, PathBuf},
};

pub(super) struct Args {
    pub data_dir: Option<std::path::PathBuf>,
    pub out: Option<std::path::PathBuf>,
    pub keep_days: f64,
}
pub(super) enum Parsed {
    Args(Args),
    Help,
    Error(String),
}
const OPTIONS: [&str; 4] = ["--help", "--data-dir", "--out", "--keep-days"];

pub(super) fn width() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|raw| {
            let value = normalized_number(&raw)?;
            let digits = value.strip_prefix('+').unwrap_or(&value);
            if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
                return None;
            }
            Some(digits.parse::<usize>().unwrap_or(usize::MAX))
        })
        .filter(|value| *value > 0)
        .unwrap_or(80)
        .saturating_sub(2)
}

fn option(raw: &str) -> Result<Option<&'static str>, String> {
    let name = raw.split_once('=').map_or(raw, |(name, _)| name);
    if name.starts_with("-h") && !name.starts_with("--") {
        return Ok(Some("--help"));
    }
    if !name.starts_with("--") {
        return Ok(None);
    }
    if let Some(option) = OPTIONS.iter().find(|option| **option == name) {
        return Ok(Some(*option));
    }
    let matches: Vec<_> = OPTIONS
        .iter()
        .copied()
        .filter(|option| option.starts_with(name))
        .collect();
    match matches.as_slice() {
        [] => Ok(None),
        [option] => Ok(Some(*option)),
        _ => Err(format!(
            "ambiguous option: {raw} could match {}",
            matches.join(", ")
        )),
    }
}

fn optional(raw: &str) -> bool {
    raw.starts_with('-')
        && raw != "-"
        && !raw.contains(' ')
        && !regex::Regex::new(r"^-(?:\d+|\d*\.\d+)$")
            .unwrap()
            .is_match(raw)
}

pub(super) fn parse(arguments: &[OsString]) -> Option<Parsed> {
    let values: Vec<_> = arguments
        .iter()
        .map(|value| value.to_str())
        .collect::<Option<_>>()?;
    // argparse classifies the full argv before executing actions (including help).
    for raw in values.iter().take_while(|raw| **raw != "--") {
        if let Err(message) = option(raw) {
            return Some(Parsed::Error(message));
        }
    }
    let mut args = Args {
        data_dir: None,
        out: None,
        keep_days: 7.0,
    };
    let mut unknown = Vec::new();
    let mut index = 0;
    while index < values.len() {
        let raw = values[index];
        if raw == "--" {
            unknown.extend_from_slice(&values[index..]);
            break;
        }
        let matched = option(raw).ok()?;
        let Some(matched) = matched else {
            unknown.push(raw);
            index += 1;
            continue;
        };
        let inline = raw.split_once('=').map(|(_, value)| value);
        if matched == "--help" {
            if let Some(value) = inline {
                return Some(Parsed::Error(format!(
                    "argument -h/--help: ignored explicit argument {}",
                    super::super::mode_repr(value)
                )));
            }
            return Some(Parsed::Help);
        }
        let value = if let Some(value) = inline {
            value
        } else {
            index += 1;
            let Some(value) = values.get(index).filter(|value| !optional(value)) else {
                return Some(Parsed::Error(format!(
                    "argument {matched}: expected one argument"
                )));
            };
            value
        };
        match matched {
            "--data-dir" => args.data_dir = Some(pathlib(Path::new(value))),
            "--out" => args.out = Some(pathlib(Path::new(value))),
            "--keep-days" => match float(value) {
                Some(value) => args.keep_days = value,
                None => {
                    return Some(Parsed::Error(format!(
                        "argument --keep-days: invalid float value: {}",
                        super::super::mode_repr(value)
                    )));
                }
            },
            _ => unreachable!(),
        }
        index += 1;
    }
    Some(if unknown.is_empty() {
        Parsed::Args(args)
    } else {
        Parsed::Error(format!("unrecognized arguments: {}", unknown.join(" ")))
    })
}

fn normalized_number(raw: &str) -> Option<String> {
    // Python 3.11 float accepts decimal Unicode digits and underscores only
    // between digits; compatibility normalization would also accept fractions
    // and superscripts, which Python rejects. These are its Unicode Nd zeros.
    const ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16ac0,
        0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    let normalized: String = raw
        .trim()
        .chars()
        .map(|c| {
            ZEROS
                .iter()
                .find_map(|zero| {
                    let digit = (c as u32).checked_sub(*zero)?;
                    (digit < 10).then(|| char::from(b'0' + digit as u8))
                })
                .unwrap_or(c)
        })
        .collect();
    let bytes = normalized.as_bytes();
    if bytes.iter().enumerate().any(|(index, c)| {
        *c == b'_'
            && (index == 0
                || index + 1 == bytes.len()
                || !bytes[index - 1].is_ascii_digit()
                || !bytes[index + 1].is_ascii_digit())
    }) {
        return None;
    }
    Some(normalized.replace('_', ""))
}

fn float(raw: &str) -> Option<f64> {
    let normalized = normalized_number(raw)?;
    let spelling = normalized.strip_prefix(['+', '-']).unwrap_or(&normalized);
    if !["nan", "inf", "infinity"]
        .iter()
        .any(|word| spelling.eq_ignore_ascii_case(word))
        && !regex::Regex::new(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
            .unwrap()
            .is_match(spelling)
    {
        return None;
    }
    normalized.parse().ok()
}
fn wrapped_parts(parts: &[&str], width: usize, indent: usize, first: usize) -> String {
    let mut output = String::new();
    let mut used = first;
    for (i, part) in parts.iter().enumerate() {
        if i > 0 && used + 1 + part.len() > width {
            output.push('\n');
            output.push_str(&" ".repeat(indent));
            used = indent;
        } else if i > 0 {
            output.push(' ');
            used += 1;
        }
        output.push_str(part);
        used += part.len();
    }
    output
}

pub(super) fn usage(width: usize) -> String {
    let prog = "pseudolife-mcp backup";
    let options = [
        "[-h]",
        "[--data-dir DATA_DIR]",
        "[--out OUT]",
        "[--keep-days KEEP_DAYS]",
    ];
    if 7 + prog.len() + 1 + options.join(" ").len() <= width {
        format!("usage: {prog} {}\n", options.join(" "))
    } else if ((7 + prog.len()) as u128) * 4 <= (width as u128) * 3 {
        let mut parts = vec![prog];
        parts.extend(options);
        format!(
            "usage: {}\n",
            wrapped_parts(&parts, width, 7 + prog.len() + 1, 7)
        )
    } else {
        format!(
            "usage: {prog}\n       {}\n",
            wrapped_parts(&options, width, 7, 7)
        )
    }
}

fn help_lines(text: &str, width: usize) -> Vec<String> {
    let mut chunks = Vec::new();
    for word in text.split_whitespace() {
        if matches!(word, "per-user" | "data-dir") || word.starts_with("<data-dir>/backups") {
            let (left, right) = word.split_once('-').unwrap();
            chunks.push(format!("{left}-"));
            chunks.push(right.into());
        } else {
            chunks.push(word.into());
        }
        chunks.push(" ".into());
    }
    chunks.pop();
    let mut lines = Vec::new();
    let mut line = String::new();
    for mut chunk in chunks {
        if chunk == " " && line.is_empty() {
            continue;
        }
        while line.len() + chunk.len() > width {
            let preserve_space = chunk.len() > width && line.len() == width;
            if chunk.len() > width {
                let count = width.saturating_sub(line.len());
                if count > 0 {
                    line.push_str(&chunk[..count]);
                    chunk = chunk[count..].into();
                }
            }
            if !line.is_empty() {
                // textwrap drops the empty long-word fragment in this case,
                // leaving the preceding whitespace chunk on the line.
                lines.push(if preserve_space {
                    line.clone()
                } else {
                    line.trim_end().into()
                });
                line.clear();
            }
            if chunk == " " {
                chunk.clear();
                break;
            }
        }
        line.push_str(&chunk);
    }
    if !line.is_empty() {
        lines.push(line.trim_end().into());
    }
    lines
}

pub(super) fn help(width: usize) -> String {
    if width == 78 {
        return super::HELP.into();
    }
    let descriptions = [
        ("-h, --help", "show this help message and exit"),
        (
            "--data-dir DATA_DIR",
            "bank data dir (default: PSEUDOLIFE_MCP_DATA_DIR, the lite tier's per-user dir, or ./data)",
        ),
        ("--out OUT", "destination dir (default: <data-dir>/backups)"),
        (
            "--keep-days KEEP_DAYS",
            "rotate this tool's own backups older than N days (default 7)",
        ),
    ];
    let position = 24.min(width.saturating_sub(20).max(4));
    let action_width = position.saturating_sub(4);
    let help_width = width.saturating_sub(position).max(11);
    let description = help_lines(
        "Back up the memory bank (pg_dump + state archive).",
        width.max(11),
    )
    .join("\n");
    let mut output = format!("{}\n{description}\n\noptions:\n", usage(width));
    for (action, description) in descriptions {
        if description.is_empty() {
            output.push_str(&format!("  {action}\n"));
            continue;
        }
        let lines = help_lines(description, help_width);
        if action.len() <= action_width {
            output.push_str(&format!("  {action:action_width$}  {}\n", lines[0]));
        } else {
            output.push_str(&format!(
                "  {action}\n{}{}\n",
                " ".repeat(position),
                lines[0]
            ));
        }
        for line in &lines[1..] {
            output.push_str(&format!("{}{line}\n", " ".repeat(position)));
        }
    }
    output
}

/// pathlib's spelling of a path, as Python's diagnostics print it:
/// components rejoined with the native separator, `.` components dropped
/// (an empty result is `.`), and on POSIX exactly two leading slashes kept.
pub(super) fn pathlib(path: &Path) -> PathBuf {
    let mut spelled: PathBuf = path
        .components()
        .filter(|component| !matches!(component, Component::CurDir))
        .collect();
    if spelled.as_os_str().is_empty() {
        spelled = PathBuf::from(".");
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let raw = path.as_os_str().as_bytes();
        if raw.starts_with(b"//") && !raw.starts_with(b"///") {
            let mut doubled = OsString::from("/");
            doubled.push(spelled.as_os_str());
            spelled = PathBuf::from(doubled);
        }
    }
    spelled
}
