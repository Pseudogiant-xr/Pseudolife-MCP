//! CPython 3.11 `json.loads` (C scanner) and `json.dumps` for the transfer leaf.
//!
//! Objects keep Python dict semantics: first position, last value. Anything the
//! oracle would reject, or would decode into a value this leaf does not carry
//! (a lone surrogate, an integer past the 4,300-digit limit, a document deeper
//! than [`MAX_DEPTH`]), is `None`, which callers turn into a deferral.
use std::collections::HashMap;
use std::fmt::Write as _;

/// CPython's default `sys.int_info.default_max_str_digits`.
const MAX_INT_DIGITS: usize = 4300;
/// Deeper documents defer instead of approximating CPython's recursion limit.
const MAX_DEPTH: usize = 200;

#[derive(Clone, Debug, PartialEq)]
pub(super) enum Value {
    Null,
    Bool(bool),
    /// `str(int(token))`: the token's digits, with `-0` folded to `0`.
    Int(String),
    Float(f64),
    Str(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

impl Value {
    pub(super) fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(members) => members.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

pub(super) fn loads(text: &str) -> Option<Value> {
    let mut parser = Parser {
        bytes: text.as_bytes(),
        text,
        at: 0,
    };
    parser.whitespace();
    let value = parser.value(0)?;
    parser.whitespace();
    (parser.at == parser.bytes.len()).then_some(value)
}

struct Parser<'a> {
    text: &'a str,
    bytes: &'a [u8],
    at: usize,
}

impl Parser<'_> {
    fn whitespace(&mut self) {
        while matches!(self.bytes.get(self.at), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn literal(&mut self, word: &str) -> bool {
        if self.bytes[self.at..].starts_with(word.as_bytes()) {
            self.at += word.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self, depth: usize) -> Option<Value> {
        match *self.bytes.get(self.at)? {
            b'{' => self.object(depth + 1),
            b'[' => self.array(depth + 1),
            b'"' => self.string().map(Value::Str),
            b'n' => self.literal("null").then_some(Value::Null),
            b't' => self.literal("true").then_some(Value::Bool(true)),
            b'f' => self.literal("false").then_some(Value::Bool(false)),
            b'N' => self.literal("NaN").then_some(Value::Float(f64::NAN)),
            b'I' => self
                .literal("Infinity")
                .then_some(Value::Float(f64::INFINITY)),
            b'-' if self.bytes[self.at..].starts_with(b"-Infinity") => {
                self.at += "-Infinity".len();
                Some(Value::Float(f64::NEG_INFINITY))
            }
            b'-' | b'0'..=b'9' => self.number(),
            _ => None,
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.at;
        while self.bytes.get(self.at).is_some_and(u8::is_ascii_digit) {
            self.at += 1;
        }
        self.at - start
    }

    fn number(&mut self) -> Option<Value> {
        let start = self.at;
        if self.bytes[self.at] == b'-' {
            self.at += 1;
        }
        match self.bytes.get(self.at) {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return None,
        }
        let integral_end = self.at;
        let mut float = false;
        if self.bytes.get(self.at) == Some(&b'.')
            && self.bytes.get(self.at + 1).is_some_and(u8::is_ascii_digit)
        {
            self.at += 1;
            self.digits();
            float = true;
        }
        if matches!(self.bytes.get(self.at), Some(b'e' | b'E')) {
            let mark = self.at;
            self.at += 1;
            if matches!(self.bytes.get(self.at), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if self.digits() == 0 {
                self.at = mark;
            } else {
                float = true;
            }
        }
        let token = &self.text[start..self.at];
        if float {
            return token.parse::<f64>().ok().map(Value::Float);
        }
        let digits = &self.text[start..integral_end];
        if digits.trim_start_matches('-').len() > MAX_INT_DIGITS {
            return None;
        }
        Some(Value::Int(if digits == "-0" {
            "0".into()
        } else {
            digits.into()
        }))
    }

    fn hex4(&mut self) -> Option<u32> {
        let raw = self.text.get(self.at..self.at + 4)?;
        if !raw.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        self.at += 4;
        u32::from_str_radix(raw, 16).ok()
    }

    fn string(&mut self) -> Option<String> {
        self.at += 1;
        let mut out = String::new();
        loop {
            let start = self.at;
            while let Some(&byte) = self.bytes.get(self.at) {
                if byte == b'"' || byte == b'\\' || byte < 0x20 {
                    break;
                }
                self.at += 1;
            }
            out.push_str(&self.text[start..self.at]);
            match *self.bytes.get(self.at)? {
                b'"' => {
                    self.at += 1;
                    return Some(out);
                }
                b'\\' => {
                    self.at += 1;
                    let escape = *self.bytes.get(self.at)?;
                    self.at += 1;
                    match escape {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let first = self.hex4()?;
                            let point = if (0xd800..0xdc00).contains(&first)
                                && self.bytes[self.at..].starts_with(b"\\u")
                            {
                                let mark = self.at;
                                self.at += 2;
                                let second = self.hex4()?;
                                if (0xdc00..0xe000).contains(&second) {
                                    0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00)
                                } else {
                                    // CPython keeps a lone high surrogate; this leaf does not.
                                    self.at = mark;
                                    first
                                }
                            } else {
                                first
                            };
                            out.push(char::from_u32(point)?);
                        }
                        _ => return None,
                    }
                }
                _ => return None, // strict: raw control characters are refused
            }
        }
    }

    fn array(&mut self, depth: usize) -> Option<Value> {
        if depth > MAX_DEPTH {
            return None;
        }
        self.at += 1;
        self.whitespace();
        let mut items = Vec::new();
        if self.bytes.get(self.at) == Some(&b']') {
            self.at += 1;
            return Some(Value::Array(items));
        }
        loop {
            items.push(self.value(depth)?);
            self.whitespace();
            match *self.bytes.get(self.at)? {
                b',' => {
                    self.at += 1;
                    self.whitespace();
                }
                b']' => {
                    self.at += 1;
                    return Some(Value::Array(items));
                }
                _ => return None,
            }
        }
    }

    fn object(&mut self, depth: usize) -> Option<Value> {
        if depth > MAX_DEPTH {
            return None;
        }
        self.at += 1;
        self.whitespace();
        let mut members: Vec<(String, Value)> = Vec::new();
        let mut index: HashMap<String, usize> = HashMap::new();
        if self.bytes.get(self.at) == Some(&b'}') {
            self.at += 1;
            return Some(Value::Object(members));
        }
        loop {
            if self.bytes.get(self.at) != Some(&b'"') {
                return None;
            }
            let key = self.string()?;
            self.whitespace();
            if self.bytes.get(self.at) != Some(&b':') {
                return None;
            }
            self.at += 1;
            self.whitespace();
            let value = self.value(depth)?;
            match index.get(&key) {
                Some(&slot) => members[slot].1 = value,
                None => {
                    index.insert(key.clone(), members.len());
                    members.push((key, value));
                }
            }
            self.whitespace();
            match *self.bytes.get(self.at)? {
                b',' => {
                    self.at += 1;
                    self.whitespace();
                }
                b'}' => {
                    self.at += 1;
                    return Some(Value::Object(members));
                }
                _ => return None,
            }
        }
    }
}

/// `float.__repr__` for a finite value; `nan`/`inf`/`-inf` otherwise.
pub(super) fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.into();
    }
    let mut out = String::new();
    if value == 0.0 {
        out.push_str(if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        });
        return out;
    }
    // CPython's shortest digits (ties to even), placed in repr's 'r'
    // exponent window.
    let (significant, power) = shortest_digits(value.abs());
    let significant = significant.as_str();
    if value.is_sign_negative() {
        out.push('-');
    }
    if !(-4..16).contains(&power) {
        out.push_str(&significant[..1]);
        if significant.len() > 1 {
            out.push('.');
            out.push_str(&significant[1..]);
        }
        let _ = write!(
            out,
            "e{}{:02}",
            if power < 0 { '-' } else { '+' },
            power.abs()
        );
    } else {
        let point = power + 1;
        if point <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-point) as usize));
            out.push_str(significant);
        } else if point as usize >= significant.len() {
            out.push_str(significant);
            out.extend(std::iter::repeat_n('0', point as usize - significant.len()));
            out.push_str(".0");
        } else {
            out.push_str(&significant[..point as usize]);
            out.push('.');
            out.push_str(&significant[point as usize..]);
        }
    }
    out
}

/// Mantissa digits and decimal exponent of `format!("{:e}")`.
fn scientific(text: &str) -> (String, i32) {
    let (mantissa, power) = text.split_once('e').unwrap_or((text, "0"));
    (
        mantissa.chars().filter(|c| *c != '.').collect(),
        power.parse().unwrap_or(0),
    )
}

/// The significant digits (no trailing zeros) and decimal exponent of
/// CPython's repr of a positive finite `value` (David Gay's mode 0): the
/// shortest digit string that reads back as `value` and, among those, the
/// one nearest to it, an exact tie going to the even digit. Rust's shortest
/// formatting fixes the digit count but may pick the other neighbour on a
/// tie (`562949953421312.25` is `.2` in Python, `.3` in Rust); the digits
/// here are the correctly rounded (half to even) prefix of the exact binary
/// value whenever that prefix reads back, else Rust's own. Same algorithm
/// as the pairing leaf's `pyjson::float_repr`, verified against the same
/// CPython tables.
fn shortest_digits(value: f64) -> (String, i32) {
    let (rust, rust_exponent) = scientific(&format!("{value:e}"));
    let rust = rust.trim_end_matches('0').to_owned();
    let count = rust.len().max(1);
    // Every binary64 has a finite decimal expansion of at most 767
    // significant digits: this precision prints it exactly.
    let (exact, exponent) = scientific(&format!("{value:.800e}"));
    let exact = exact.as_bytes();
    let mut kept: Vec<u8> = exact[..count].to_vec();
    let rest = &exact[count..];
    let up = match rest.first() {
        Some(b'6'..=b'9') => true,
        Some(b'5') => rest[1..].iter().any(|d| *d != b'0') || kept[count - 1] % 2 == 1,
        _ => false,
    };
    let mut exponent = exponent;
    if up {
        let mut index = count;
        loop {
            if index == 0 {
                kept.insert(0, b'1');
                kept.pop();
                exponent += 1;
                break;
            }
            index -= 1;
            if kept[index] == b'9' {
                kept[index] = b'0';
            } else {
                kept[index] += 1;
                break;
            }
        }
    }
    let nearest = String::from_utf8(kept).unwrap_or_default();
    let candidate = format!("{}.{}e{exponent}", &nearest[..1], &nearest[1..]);
    if candidate.parse::<f64>().ok() == Some(value) {
        (nearest.trim_end_matches('0').to_owned(), exponent)
    } else {
        (rust, rust_exponent)
    }
}

/// The `json` encoder's float spelling (`allow_nan=True`).
pub(super) fn json_float(value: f64) -> String {
    if value.is_nan() {
        "NaN".into()
    } else if value.is_infinite() {
        if value < 0.0 { "-Infinity" } else { "Infinity" }.into()
    } else {
        float_repr(value)
    }
}

pub(super) fn write_str(out: &mut String, text: &str, ascii: bool) {
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

/// `json.dumps(value, ensure_ascii=ascii, indent=indent)`.
pub(super) fn dumps(value: &Value, ascii: bool, indent: Option<usize>) -> String {
    let mut out = String::new();
    write(&mut out, value, ascii, indent, 0);
    out
}

fn newline(out: &mut String, indent: usize, level: usize) {
    out.push('\n');
    out.extend(std::iter::repeat_n(' ', indent * level));
}

fn write(out: &mut String, value: &Value, ascii: bool, indent: Option<usize>, level: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(digits) => out.push_str(digits),
        Value::Float(value) => out.push_str(&json_float(*value)),
        Value::Str(text) => write_str(out, text, ascii),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(members) if members.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                separator(out, index, indent, level);
                write(out, item, ascii, indent, level + 1);
            }
            close(out, ']', indent, level);
        }
        Value::Object(members) => {
            out.push('{');
            for (index, (key, item)) in members.iter().enumerate() {
                separator(out, index, indent, level);
                write_str(out, key, ascii);
                out.push_str(": ");
                write(out, item, ascii, indent, level + 1);
            }
            close(out, '}', indent, level);
        }
    }
}

fn separator(out: &mut String, index: usize, indent: Option<usize>, level: usize) {
    match indent {
        Some(width) => {
            if index > 0 {
                out.push(',');
            }
            newline(out, width, level + 1);
        }
        None if index > 0 => out.push_str(", "),
        None => {}
    }
}

fn close(out: &mut String, bracket: char, indent: Option<usize>, level: usize) {
    if let Some(width) = indent {
        newline(out, width, level);
    }
    out.push(bracket);
}

/// True when any string (key or value) holds U+0000 or any float is not finite:
/// PostgreSQL's jsonb input refuses both after `json.dumps`.
pub(super) fn jsonb_unloadable(value: &Value) -> bool {
    match value {
        Value::Float(value) => !value.is_finite(),
        Value::Str(text) => text.contains('\0'),
        Value::Array(items) => items.iter().any(jsonb_unloadable),
        Value::Object(members) => members
            .iter()
            .any(|(key, item)| key.contains('\0') || jsonb_unloadable(item)),
        Value::Null | Value::Bool(_) | Value::Int(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python_windows() {
        for (value, spelled) in [
            (150.0, "150.0"),
            (1e-8, "1e-08"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (-0.0, "-0.0"),
            (0.1234567, "0.1234567"),
            (-3.4028234663852886e38, "-3.4028234663852886e+38"),
            (5e-324, "5e-324"),
            (123456789.125, "123456789.125"),
            (1.5e300, "1.5e+300"),
            // 2^49 + 0.25: an exact tie at sixteen digits, even digit kept.
            (562_949_953_421_312.0 + 0.25, "562949953421312.2"),
        ] {
            assert_eq!(float_repr(value), spelled);
        }
        assert_eq!(json_float(f64::NAN), "NaN");
        assert_eq!(json_float(f64::NEG_INFINITY), "-Infinity");
    }

    /// CPython 3.11's repr of binary64 values whose exact decimal expansion
    /// ties at the shortest length (the pairing leaf's table), plus master's
    /// random and boundary table.
    #[test]
    fn float_repr_matches_cpython_tables() {
        let mut wrong = Vec::new();
        let mut checked = 0;
        for table in [
            include_str!("../pairing/float_repr_ties.tsv"),
            include_str!("../../../tests/cli_audit_float_repr.tsv"),
        ] {
            for line in table
                .lines()
                .filter(|l| !l.starts_with('#') && !l.is_empty())
            {
                let (bits, expected) = line.split_once('\t').expect("bits TAB repr");
                let value = f64::from_bits(u64::from_str_radix(bits, 16).unwrap());
                checked += 1;
                if float_repr(value) != expected {
                    wrong.push(format!("{bits}: {} != {expected}", float_repr(value)));
                }
            }
        }
        assert!(checked > 7000, "{checked} rows");
        assert!(wrong.is_empty(), "{} of {checked}: {wrong:?}", wrong.len());
    }

    #[test]
    fn loads_keeps_python_dict_and_number_rules() {
        let value = loads(r#"{"b": 1, "a": [1.50, -0, 1E5, 2e], "b": 3}"#);
        assert_eq!(value, None, "a dangling exponent is extra data");
        let value = loads(r#" {"b": 1, "a": [1.50, -0, 1E5], "b": 3} "#).unwrap();
        assert_eq!(
            dumps(&value, false, None),
            r#"{"b": 3, "a": [1.5, 0, 100000.0]}"#
        );
        assert_eq!(loads("[01]"), None);
        assert_eq!(loads("\"\\ud800\""), None);
        assert_eq!(loads("\"\u{1}\""), None);
        assert_eq!(
            loads("\"\\ud83d\\ude00\""),
            Some(Value::Str("\u{1f600}".into()))
        );
        assert!(matches!(loads("NaN"), Some(Value::Float(v)) if v.is_nan()));
        assert_eq!(loads(&"9".repeat(4301)), None);
        assert!(loads(&"9".repeat(4300)).is_some());
    }

    #[test]
    fn dumps_escapes_and_indents_like_python() {
        let value = Value::Object(vec![
            (
                "k\u{2028}".into(),
                Value::Str("\u{7f}é\u{1f600}\u{1}".into()),
            ),
            ("e".into(), Value::Array(vec![])),
            ("n".into(), Value::Array(vec![Value::Int("1".into())])),
        ]);
        assert_eq!(
            dumps(&value, false, None),
            "{\"k\u{2028}\": \"\u{7f}é\u{1f600}\\u0001\", \"e\": [], \"n\": [1]}"
        );
        assert_eq!(
            dumps(&value, true, Some(2)),
            "{\n  \"k\\u2028\": \"\\u007f\\u00e9\\ud83d\\ude00\\u0001\",\n  \"e\": [],\n  \"n\": [\n    1\n  ]\n}"
        );
    }
}
