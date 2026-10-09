//! Python `json` and `str()` semantics for client configuration files.
//!
//! Values keep Python's normalized number spelling (`int(lexeme)` and
//! `repr(float(lexeme))`) and dict insertion order; a duplicate key keeps its
//! first position with its last value, as a Python dict does. Inputs whose
//! Python reading this module cannot reproduce exactly (NaN/Infinity, lone
//! surrogate escapes, very deep nesting, over-long integers, non-finite
//! floats) defer.
use super::Defer;
use std::fmt::Write as _;

#[derive(Clone, Debug, PartialEq)]
pub(super) enum J {
    Null,
    Bool(bool),
    Int(String),
    Float(f64),
    Str(String),
    List(Vec<J>),
    Dict(Vec<(String, J)>),
}

pub(super) type Dict = Vec<(String, J)>;

/// `d.get(key)` with Python's `None` for both an absent key and JSON null.
pub(super) fn get<'a>(dict: &'a Dict, key: &str) -> Option<&'a J> {
    match dict.iter().find(|(k, _)| k == key).map(|(_, v)| v) {
        Some(J::Null) | None => None,
        Some(value) => Some(value),
    }
}

/// `key in d`.
pub(super) fn has(dict: &Dict, key: &str) -> bool {
    dict.iter().any(|(k, _)| k == key)
}

/// `d[key] = value`: an existing key keeps its position.
pub(super) fn set(dict: &mut Dict, key: &str, value: J) {
    match dict.iter_mut().find(|(k, _)| k == key) {
        Some(slot) => slot.1 = value,
        None => dict.push((key.to_owned(), value)),
    }
}

/// `d.pop(key, None)`.
pub(super) fn remove(dict: &mut Dict, key: &str) {
    dict.retain(|(k, _)| k != key);
}

pub(super) fn get_mut<'a>(dict: &'a mut Dict, key: &str) -> Option<&'a mut J> {
    dict.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v)
}

impl J {
    pub(super) fn truthy(&self) -> bool {
        match self {
            J::Null => false,
            J::Bool(value) => *value,
            J::Int(value) => value != "0",
            J::Float(value) => *value != 0.0,
            J::Str(value) => !value.is_empty(),
            J::List(values) => !values.is_empty(),
            J::Dict(values) => !values.is_empty(),
        }
    }

    pub(super) fn as_dict(&self) -> Option<&Dict> {
        match self {
            J::Dict(values) => Some(values),
            _ => None,
        }
    }

    pub(super) fn as_str(&self) -> Option<&str> {
        match self {
            J::Str(value) => Some(value),
            _ => None,
        }
    }

    /// `str(value)` for scalars; a container's repr defers.
    pub(super) fn py_str(&self) -> Result<String, Defer> {
        Ok(match self {
            J::Null => "None".to_owned(),
            J::Bool(true) => "True".to_owned(),
            J::Bool(false) => "False".to_owned(),
            J::Int(value) => value.clone(),
            J::Float(value) => float_repr(*value),
            J::Str(value) => value.clone(),
            J::List(_) | J::Dict(_) => return Err(Defer),
        })
    }
}

/// Python's `str.isspace()`.
pub(super) fn py_space(c: char) -> bool {
    matches!(
        c,
        '\t' | '\n'
            | '\u{b}'
            | '\u{c}'
            | '\r'
            | '\u{1c}'..='\u{1f}'
            | ' '
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

/// Python's `str.strip()` with no argument.
pub(super) fn py_strip(text: &str) -> &str {
    text.trim_matches(py_space)
}

const MAX_DEPTH: usize = 100;
// CPython 3.11's int_max_str_digits default.
const MAX_INT_DIGITS: usize = 4300;

fn convert(value: serde_json::Value, depth: usize) -> Result<J, Defer> {
    if depth > MAX_DEPTH {
        return Err(Defer);
    }
    Ok(match value {
        serde_json::Value::Null => J::Null,
        serde_json::Value::Bool(value) => J::Bool(value),
        serde_json::Value::Number(number) => {
            let lexeme = number.as_str();
            if lexeme.contains(['.', 'e', 'E']) {
                let parsed: f64 = lexeme.parse().map_err(|_| Defer)?;
                if !parsed.is_finite() {
                    return Err(Defer);
                }
                J::Float(parsed)
            } else {
                let digits = lexeme.strip_prefix('-').unwrap_or(lexeme);
                if digits.len() > MAX_INT_DIGITS {
                    return Err(Defer);
                }
                if digits.bytes().all(|b| b == b'0') {
                    J::Int("0".to_owned())
                } else {
                    J::Int(lexeme.to_owned())
                }
            }
        }
        serde_json::Value::String(value) => J::Str(value),
        serde_json::Value::Array(values) => J::List(
            values
                .into_iter()
                .map(|value| convert(value, depth + 1))
                .collect::<Result<_, _>>()?,
        ),
        serde_json::Value::Object(map) => J::Dict(
            map.into_iter()
                .map(|(key, value)| Ok((key, convert(value, depth + 1)?)))
                .collect::<Result<_, Defer>>()?,
        ),
    })
}

/// `json.loads(text)`: `Ok(None)` when Python raises a JSONDecodeError.
pub(super) fn loads(text: &str) -> Result<Option<J>, Defer> {
    match serde_json::from_str::<serde_json::Value>(text) {
        Ok(value) => convert(value, 0).map(Some),
        Err(error) => {
            // Python accepts what serde_json refuses here; never guess.
            if error.to_string().contains("recursion limit")
                || text.contains("NaN")
                || text.contains("Infinity")
                || has_surrogate_escape(text)
            {
                Err(Defer)
            } else {
                Ok(None)
            }
        }
    }
}

fn has_surrogate_escape(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.windows(4).any(|window| {
        window[0] == b'\\'
            && window[1] == b'u'
            && matches!(window[2], b'd' | b'D')
            && matches!(window[3], b'8'..=b'9' | b'a'..=b'f' | b'A'..=b'F')
    })
}

/// Python float repr (`float_repr_style == 'short'`).
pub(super) fn float_repr(value: f64) -> String {
    let mut out = String::new();
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.to_owned();
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .to_owned();
    }
    // The same reshaping as sent_json.rs: Rust's shortest round-trip digits
    // placed in repr's exponent window.
    let text = format!("{:e}", value.abs());
    let (mantissa, power) = text.split_once('e').unwrap_or((text.as_str(), "0"));
    let power: i32 = power.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let significant = digits.trim_end_matches('0');
    let significant = if significant.is_empty() {
        "0"
    } else {
        significant
    };
    let exponent = power;
    if value.is_sign_negative() {
        out.push('-');
    }
    if !(-4..16).contains(&exponent) {
        out.push_str(&significant[..1]);
        if significant.len() > 1 {
            out.push('.');
            out.push_str(&significant[1..]);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        let _ = write!(out, "e{sign}{:02}", exponent.abs());
    } else {
        let point = exponent + 1;
        if point <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-point) as usize));
            out.push_str(significant);
        } else if point as usize >= significant.len() {
            out.push_str(significant);
            out.extend(std::iter::repeat_n('0', point as usize - significant.len()));
            out.push_str(".0");
        } else {
            let point = point as usize;
            out.push_str(&significant[..point]);
            out.push('.');
            out.push_str(&significant[point..]);
        }
    }
    out
}

fn quoted(text: &str, ascii: bool, out: &mut String) {
    out.push('"');
    for value in text.chars() {
        match value {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\u{0}'..='\u{1f}' => {
                let _ = write!(out, "\\u{:04x}", value as u32);
            }
            '\u{20}'..='\u{7e}' => out.push(value),
            value if !ascii => out.push(value),
            value => {
                let point = value as u32;
                if point <= 0xffff {
                    let _ = write!(out, "\\u{point:04x}");
                } else {
                    let point = point - 0x10000;
                    let _ = write!(
                        out,
                        "\\u{:04x}\\u{:04x}",
                        0xd800 + (point >> 10),
                        0xdc00 + (point & 0x3ff)
                    );
                }
            }
        }
    }
    out.push('"');
}

fn append(value: &J, ascii: bool, level: usize, out: &mut String) {
    let pad = |out: &mut String, level: usize| {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', level * 2));
    };
    match value {
        J::Null => out.push_str("null"),
        J::Bool(value) => out.push_str(if *value { "true" } else { "false" }),
        J::Int(value) => out.push_str(value),
        J::Float(value) => out.push_str(&float_repr(*value)),
        J::Str(value) => quoted(value, ascii, out),
        J::List(values) if values.is_empty() => out.push_str("[]"),
        J::List(values) => {
            out.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                pad(out, level + 1);
                append(value, ascii, level + 1, out);
            }
            pad(out, level);
            out.push(']');
        }
        J::Dict(values) if values.is_empty() => out.push_str("{}"),
        J::Dict(values) => {
            out.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                pad(out, level + 1);
                quoted(key, ascii, out);
                out.push_str(": ");
                append(value, ascii, level + 1, out);
            }
            pad(out, level);
            out.push('}');
        }
    }
}

/// `json.dumps(value, indent=2, ensure_ascii=ascii)`.
pub(super) fn dumps(value: &J, ascii: bool) -> String {
    let mut out = String::new();
    append(value, ascii, 0, &mut out);
    out
}

/// Python `repr()` of a str, for the printable-ASCII domain only.
pub(super) fn py_repr(text: &str) -> Result<String, Defer> {
    if !text.chars().all(|c| (' '..='~').contains(&c)) {
        return Err(Defer);
    }
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in text.chars() {
        if c == '\\' || c == quote {
            out.push('\\');
        }
        out.push(c);
    }
    out.push(quote);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python() {
        for (value, expected) in [
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1.5, "1.5"),
            (-2.0, "-2.0"),
            (0.1, "0.1"),
            (123456.789, "123456.789"),
            (1.5e300, "1.5e+300"),
        ] {
            assert_eq!(float_repr(value), expected);
        }
    }

    #[test]
    fn pretty_dump_matches_python_layout() {
        let value = loads(r#"{"a": [1, 2.50, {}], "b": {"c": "é\u0001"}, "d": [], "a": -0}"#)
            .unwrap()
            .unwrap();
        assert_eq!(
            dumps(&value, false),
            "{\n  \"a\": 0,\n  \"b\": {\n    \"c\": \"é\\u0001\"\n  },\n  \"d\": []\n}"
        );
        assert_eq!(
            dumps(&J::Str("é😀\u{7f}".into()), true),
            "\"\\u00e9\\ud83d\\ude00\\u007f\""
        );
    }

    #[test]
    fn python_only_inputs_defer() {
        assert!(loads("{\"a\": NaN}").is_err());
        assert!(loads("{\"a\": \"\\ud800\"}").is_err());
        assert!(matches!(loads("{\"a\": }"), Ok(None)));
    }

    #[test]
    fn repr_quotes_like_python() {
        assert_eq!(py_repr("a,b").unwrap(), "'a,b'");
        assert_eq!(py_repr("it's").unwrap(), "\"it's\"");
        assert_eq!(py_repr("a'\"b").unwrap(), "'a\\'\"b'");
        assert!(py_repr("é").is_err());
    }
}
