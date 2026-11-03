//! config.yaml as PyYAML 6.0.3's `safe_load` reads it, for the config
//! editor: plain scalars resolve by `SafeLoader`'s YAML 1.1 rules (`yes`,
//! `017`, `1_000`, `1:30`, `1e5` as a string) and construct as
//! `SafeConstructor` does (`construct_yaml_bool`, `construct_yaml_int`,
//! `construct_yaml_float`). Startup uses the same scalar rules, refusing
//! consumed values its config tree cannot represent rather than rounding them.
//!
//! A timestamp constructs a Python `date` or `datetime`, which this tree
//! cannot hold: the read for `saved` keeps its text (`str(date)` is the
//! same text), and a rewrite refuses the file rather than change it.

use yaml_rust2::parser::Tag;
use yaml_rust2::scanner::TScalarStyle;

use super::yaml_dump::{
    TAG_BOOL, TAG_FLOAT, TAG_INT, TAG_MERGE, TAG_NULL, TAG_STR, TAG_TIMESTAMP, resolve,
};
use crate::config::{
    Node, UnsupportedKind, resolve_scalar, safe_load_startup_with, safe_load_with,
};

/// Startup must preserve values exactly. The editor's read-only projection
/// may display an oversized integer as a float; startup must not use it.
pub(crate) fn load_startup(text: &str) -> Result<Option<Node>, String> {
    safe_load_startup_with(text, construct_startup)
}

fn construct_startup(
    value: String,
    mut style: TScalarStyle,
    mut tag: Option<Tag>,
) -> Result<Node, String> {
    // PyYAML applies implicit resolution to a non-specific tag even when
    // its scalar is quoted.
    if tag.as_ref().is_some_and(|t| {
        (t.handle == "!" && t.suffix.is_empty()) || (t.handle.is_empty() && t.suffix == "!")
    }) {
        tag = None;
        style = TScalarStyle::Plain;
    }
    let resolved = match &tag {
        Some(t) if t.handle == "tag:yaml.org,2002:" => match t.suffix.as_str() {
            "null" => return Ok(Node::Null),
            "int" => TAG_INT,
            "timestamp" => TAG_TIMESTAMP,
            "binary" => return Ok(Node::Unsupported {
                kind: UnsupportedKind::Binary,
                truthy: value.chars().any(|c| !c.is_whitespace()),
                value,
                reason: "YAML binary values cannot be used at startup; use a plain value instead of !!binary".into(),
            }),
            _ => TAG_STR,
        },
        None if style == TScalarStyle::Plain => resolve(&value),
        _ => TAG_STR,
    };
    match resolved {
        TAG_TIMESTAMP => Ok(Node::Unsupported {
            kind: UnsupportedKind::Timestamp,
            truthy: true,
            value,
            reason: "YAML timestamps cannot be used at startup; quote the date or timestamp to use it as a string".into(),
        }),
        TAG_INT => {
            // Python constructs every scalar, including unused keys. Only a
            // valid integer beyond our range may remain an unread placeholder.
            construct_int_with(&value, |digits, radix| {
                startup_integer_text(digits, radix).map(|_| 0)
            })
            .map_err(|_| format!("cannot read {value:?} as a YAML integer; use integer digits or quote it as a string"))?;
            Ok(match construct_int_with(&value, startup_integer) {
            Ok(value) => Node::Int(value),
            Err(_) => Node::Unsupported {
                kind: UnsupportedKind::Integer,
                truthy: true,
                value,
                reason: "YAML integer is invalid or outside the signed 64-bit range; use a value from -9223372036854775808 to 9223372036854775807, or quote it for a string setting".into(),
            },
            })
        }
        _ => construct(value, style, tag, false),
    }
}

/// For `_saved_file`: a timestamp keeps its text.
pub(crate) fn load_reading(text: &str) -> Result<Option<Node>, String> {
    safe_load_with(text, |v, style, tag| construct(v, style, tag, false), None)
}

/// For `write_config`: a timestamp or binary value refuses the rewrite.
pub(crate) fn load_rewriting(text: &str) -> Result<Option<Node>, String> {
    safe_load_with(
        text,
        |v, style, tag| construct(v, style, tag, true),
        Some(SHARED),
    )
}

/// A mapping or list shared through a YAML anchor and alias: PyYAML loads
/// one object and a patch through either path changes both, which a
/// rewrite of copies cannot keep.
pub(crate) const SHARED: &str = "config.yaml shares a mapping or list through a YAML anchor and alias, which this daemon cannot rewrite; edit config.yaml on the daemon host instead";

/// An integer past 64 bits, which this tree cannot hold.
pub(crate) const BIG_INT: &str = "config.yaml holds an integer past 64 bits, which this daemon cannot rewrite; edit config.yaml on the daemon host instead";

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
        TAG_INT => match construct_int(&value) {
            Ok(i) => Ok(Node::Int(i)),
            Err(_) if rewriting => Err(BIG_INT.into()),
            // The read only coerces knob values: the nearest float.
            Err(e) => big_int_float(&value).map(Node::Float).ok_or(e),
        },
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
    construct_int_with(text, |digits, radix| {
        i128::from_str_radix(digits, radix).ok()
    })
}

/// PyYAML selects the radix before Python's `int()` consumes whitespace.
/// Thus `!!int ' 010 '` is decimal 10, while `!!int '010 '` is octal 8.
fn startup_integer(text: &str, radix: u32) -> Option<i128> {
    i128::from_str_radix(&startup_integer_text(text, radix)?, radix).ok()
}

fn startup_integer_text(text: &str, radix: u32) -> Option<String> {
    let normalized: String = text
        .trim()
        .chars()
        .map(|c| {
            crate::graph_review::decimal_value(c)
                .and_then(|d| char::from_digit(d, 10))
                .unwrap_or(c)
        })
        .collect();
    let (sign, digits) = match normalized.as_bytes().first() {
        Some(b'-') => ("-", &normalized[1..]),
        Some(b'+') => ("+", &normalized[1..]),
        _ => ("", normalized.as_str()),
    };
    let prefix = match radix {
        2 => ["0b", "0B"],
        8 => ["0o", "0O"],
        16 => ["0x", "0X"],
        _ => ["", ""],
    };
    let digits = digits
        .strip_prefix(prefix[0])
        .or_else(|| digits.strip_prefix(prefix[1]))
        .unwrap_or(digits);
    if digits.is_empty() || !digits.chars().all(|c| c.is_digit(radix)) {
        return None;
    }
    Some(format!("{sign}{digits}"))
}

fn construct_int_with(
    text: &str,
    parse: impl Fn(&str, u32) -> Option<i128>,
) -> Result<i64, String> {
    let value: String = text.chars().filter(|&c| c != '_').collect();
    let (negative, digits) = match value.as_bytes().first() {
        Some(b'-') => (true, &value[1..]),
        Some(b'+') => (false, &value[1..]),
        _ => (false, value.as_str()),
    };
    let magnitude: Option<i128> = if digits == "0" {
        Some(0)
    } else if let Some(bin) = digits.strip_prefix("0b") {
        parse(bin, 2)
    } else if let Some(hex) = digits.strip_prefix("0x") {
        parse(hex, 16)
    } else if digits.starts_with('0') {
        parse(digits, 8)
    } else if digits.contains(':') {
        digits.split(':').try_fold(0i128, |total, part| {
            total.checked_mul(60)?.checked_add(parse(part, 10)?)
        })
    } else {
        parse(digits, 10)
    };
    magnitude
        .and_then(|m| if negative { m.checked_neg() } else { Some(m) })
        .and_then(|v| i64::try_from(v).ok())
        .ok_or_else(|| format!("config.yaml holds an integer out of range: {text}"))
}

/// An integer `construct_int` cannot hold, as the float nearest it.
fn big_int_float(text: &str) -> Option<f64> {
    let digits: String = text.chars().filter(|&c| c != '_').collect();
    digits.parse::<f64>().ok().filter(|f| f.is_finite())
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
