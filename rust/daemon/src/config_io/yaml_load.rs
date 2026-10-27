//! config.yaml as PyYAML 6.0.3's `safe_load` reads it, for the config
//! editor: plain scalars resolve by `SafeLoader`'s YAML 1.1 rules (`yes`,
//! `017`, `1_000`, `1:30`, `1e5` as a string) and construct as
//! `SafeConstructor` does (`construct_yaml_bool`, `construct_yaml_int`,
//! `construct_yaml_float`). The daemon's own start-up load keeps its rules
//! (`config.rs`).
//!
//! A timestamp constructs a Python `date` or `datetime`, which this tree
//! cannot hold: the read for `saved` keeps its text (`str(date)` is the
//! same text), and a rewrite refuses the file rather than change it.

use yaml_rust2::parser::Tag;
use yaml_rust2::scanner::TScalarStyle;

use super::yaml_dump::{
    TAG_BOOL, TAG_FLOAT, TAG_INT, TAG_MERGE, TAG_NULL, TAG_STR, TAG_TIMESTAMP, resolve,
};
use crate::config::{Node, resolve_scalar, safe_load_with};

/// For `_saved_file`: a timestamp keeps its text.
pub(crate) fn load_reading(text: &str) -> Result<Option<Node>, String> {
    safe_load_with(text, |v, style, tag| construct(v, style, tag, false))
}

/// For `write_config`: a timestamp or binary value refuses the rewrite.
pub(crate) fn load_rewriting(text: &str) -> Result<Option<Node>, String> {
    safe_load_with(text, |v, style, tag| construct(v, style, tag, true))
}

/// The value this daemon cannot write back as Python would.
pub(crate) const UNWRITABLE: &str = "config.yaml holds a YAML timestamp or binary value, \
     which this daemon cannot rewrite; edit config.yaml on the daemon host instead";

fn construct(
    value: String,
    style: TScalarStyle,
    tag: Option<Tag>,
    rewriting: bool,
) -> Result<Node, String> {
    let resolved = match &tag {
        // An explicit core tag constructs as the implicit one would.
        Some(t) if t.handle == "tag:yaml.org,2002:" => match t.suffix.as_str() {
            "int" => TAG_INT,
            "float" => TAG_FLOAT,
            "bool" => TAG_BOOL,
            "timestamp" => TAG_TIMESTAMP,
            "binary" if rewriting => return Err(UNWRITABLE.into()),
            _ => return resolve_scalar(value, style, tag),
        },
        Some(_) => return resolve_scalar(value, style, tag),
        None if style != TScalarStyle::Plain => return Ok(Node::Str(value)),
        None => resolve(&value),
    };
    let bad = || format!("cannot read {value:?} as {resolved}");
    match resolved {
        TAG_NULL => Ok(Node::Null),
        TAG_BOOL => match value.to_ascii_lowercase().as_str() {
            "yes" | "true" | "on" => Ok(Node::Bool(true)),
            "no" | "false" | "off" => Ok(Node::Bool(false)),
            _ => Err(bad()),
        },
        TAG_INT => construct_int(&value).map(Node::Int),
        TAG_FLOAT => construct_float(&value).map(Node::Float).ok_or_else(bad),
        TAG_MERGE => Ok(Node::MergeKey),
        TAG_TIMESTAMP if rewriting => Err(UNWRITABLE.into()),
        TAG_TIMESTAMP | TAG_STR => Ok(Node::Str(value)),
        other => Err(format!(
            "could not determine a constructor for the tag '{other}'"
        )),
    }
}

/// `construct_yaml_int`, refused past the i64 range this tree holds.
fn construct_int(text: &str) -> Result<i64, String> {
    let value: String = text.chars().filter(|&c| c != '_').collect();
    let (negative, digits) = match value.as_bytes().first() {
        Some(b'-') => (true, &value[1..]),
        Some(b'+') => (false, &value[1..]),
        _ => (false, value.as_str()),
    };
    let magnitude: Option<i128> = if digits == "0" {
        Some(0)
    } else if let Some(bin) = digits.strip_prefix("0b") {
        i128::from_str_radix(bin, 2).ok()
    } else if let Some(hex) = digits.strip_prefix("0x") {
        i128::from_str_radix(hex, 16).ok()
    } else if digits.starts_with('0') {
        i128::from_str_radix(digits, 8).ok()
    } else if digits.contains(':') {
        digits.split(':').try_fold(0i128, |total, part| {
            total
                .checked_mul(60)?
                .checked_add(part.parse::<i128>().ok()?)
        })
    } else {
        digits.parse::<i128>().ok()
    };
    magnitude
        .map(|m| if negative { -m } else { m })
        .and_then(|v| i64::try_from(v).ok())
        .ok_or_else(|| format!("config.yaml holds an integer out of range: {text}"))
}

/// `construct_yaml_float`; `None` where Python's `float()` raises.
fn construct_float(text: &str) -> Option<f64> {
    let value: String = text
        .chars()
        .filter(|&c| c != '_')
        .collect::<String>()
        .to_lowercase();
    let (sign, body) = match value.as_bytes().first() {
        Some(b'-') => (-1.0, &value[1..]),
        Some(b'+') => (1.0, &value[1..]),
        _ => (1.0, value.as_str()),
    };
    if body == ".inf" {
        return Some(sign * f64::INFINITY);
    }
    if body == ".nan" {
        return Some(f64::NAN);
    }
    if body.contains(':') {
        let mut total = 0.0;
        for part in body.split(':') {
            total = total * 60.0 + py_float(part)?;
        }
        return Some(sign * total);
    }
    Some(sign * py_float(body)?)
}

/// Python `float(text)` (`"1."`, `".5"`, `"1.e+5"`, `"inf"`), as
/// `py_float_str` reads it.
fn py_float(text: &str) -> Option<f64> {
    let fixed = match text.find(['e', 'E']) {
        Some(i) if text[..i].ends_with('.') => format!("{}0{}", &text[..i], &text[i..]),
        _ => text.to_string(),
    };
    crate::dream::extractor::py_float_str(&fixed)
}
