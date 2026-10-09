//! Python `json` and `str()` semantics for the pairing commands' daemon
//! answers and reports (copied from the connect leaf's reader).
//!
//! The reader follows CPython's C scanner (`NaN`/`Infinity` literals,
//! strict control characters, four-digit escapes). Values keep Python's
//! number spelling (`int(lexeme)`, `repr(float(lexeme))`) and dict insertion
//! order; a duplicate key keeps its first position with its last value, as a
//! Python dict does. A lone surrogate (no Rust `String` holds it), nesting
//! past 100 and integers past 4300 digits defer.
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

/// `d[key] = value`: an existing key keeps its position.
pub(super) fn set(dict: &mut Dict, key: &str, value: J) {
    match dict.iter_mut().find(|(k, _)| k == key) {
        Some(slot) => slot.1 = value,
        None => dict.push((key.to_owned(), value)),
    }
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

    pub(super) fn as_str(&self) -> Option<&str> {
        match self {
            J::Str(value) => Some(value),
            _ => None,
        }
    }

    /// `str(value)`: a str as itself, anything else its repr.
    pub(super) fn py_str(&self) -> String {
        match self {
            J::Str(value) => value.clone(),
            other => py_value_repr(other),
        }
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

// Python's recursion limit is ~1000 frames; this reader stays well inside.
const MAX_DEPTH: usize = 100;
// CPython 3.11's int_max_str_digits default.
const MAX_INT_DIGITS: usize = 4300;

/// Why the reader stopped: Python's JSONDecodeError, or an input whose
/// Python reading cannot be held here.
enum Stop {
    Syntax,
    Defer,
}

/// CPython's C scanner (`json.loads` with its defaults).
struct Reader<'a> {
    text: &'a str,
    at: usize,
    depth: usize,
    lossy: bool,
}

impl Reader<'_> {
    fn peek(&self) -> Option<u8> {
        self.text.as_bytes().get(self.at).copied()
    }

    fn space(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn word(&mut self, word: &str) -> bool {
        if self.text[self.at..].starts_with(word) {
            self.at += word.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Result<J, Stop> {
        match self.peek() {
            Some(b'"') => self.string().map(J::Str),
            Some(b'{') => self.nested(Self::object),
            Some(b'[') => self.nested(Self::array),
            Some(b'n') if self.word("null") => Ok(J::Null),
            Some(b't') if self.word("true") => Ok(J::Bool(true)),
            Some(b'f') if self.word("false") => Ok(J::Bool(false)),
            Some(b'N') if self.word("NaN") => Ok(J::Float(f64::NAN)),
            Some(b'I') if self.word("Infinity") => Ok(J::Float(f64::INFINITY)),
            Some(b'-') if self.word("-Infinity") => Ok(J::Float(f64::NEG_INFINITY)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(Stop::Syntax),
        }
    }

    fn nested(&mut self, read: fn(&mut Self) -> Result<J, Stop>) -> Result<J, Stop> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(Stop::Defer);
        }
        let value = read(self);
        self.depth -= 1;
        value
    }

    fn digits(&mut self) -> usize {
        let start = self.at;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.at += 1;
        }
        self.at - start
    }

    /// `(-?(?:0|[1-9]\d*))(\.\d+)?([eE][-+]?\d+)?`
    fn number(&mut self) -> Result<J, Stop> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        match self.peek() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return Err(Stop::Syntax),
        }
        let integer_end = self.at;
        let mut float = false;
        if self.peek() == Some(b'.') {
            let dot = self.at;
            self.at += 1;
            if self.digits() == 0 {
                self.at = dot;
            } else {
                float = true;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let mark = self.at;
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if self.digits() == 0 {
                self.at = mark;
            } else {
                float = true;
            }
        }
        let lexeme = &self.text[start..self.at];
        if float {
            return lexeme.parse::<f64>().map(J::Float).map_err(|_| Stop::Defer);
        }
        let digits = &self.text[start..integer_end];
        let magnitude = digits.strip_prefix('-').unwrap_or(digits);
        if magnitude.len() > MAX_INT_DIGITS {
            return Err(Stop::Defer);
        }
        Ok(J::Int(if magnitude == "0" {
            "0".to_owned()
        } else {
            digits.to_owned()
        }))
    }

    fn hex4(&mut self) -> Result<u32, Stop> {
        let digits = self.text.get(self.at..self.at + 4).ok_or(Stop::Syntax)?;
        if !digits.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(Stop::Syntax);
        }
        self.at += 4;
        u32::from_str_radix(digits, 16).map_err(|_| Stop::Syntax)
    }

    fn string(&mut self) -> Result<String, Stop> {
        self.at += 1;
        let mut out = String::new();
        loop {
            let rest = &self.text[self.at..];
            let c = rest.chars().next().ok_or(Stop::Syntax)?;
            self.at += c.len_utf8();
            match c {
                '"' => return Ok(out),
                '\u{0}'..='\u{1f}' => return Err(Stop::Syntax),
                '\\' => {
                    let escape = self.peek().ok_or(Stop::Syntax)?;
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
                            let unit = self.hex4()?;
                            let point = if (0xd800..0xdc00).contains(&unit)
                                && self.text[self.at..].starts_with("\\u")
                            {
                                let mark = self.at;
                                self.at += 2;
                                let low = self.hex4()?;
                                if (0xdc00..0xe000).contains(&low) {
                                    0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00)
                                } else {
                                    self.at = mark;
                                    unit
                                }
                            } else {
                                unit
                            };
                            // A lone surrogate is a Python str no Rust String holds.
                            out.push(match char::from_u32(point) {
                                Some(c) => c,
                                None if self.lossy => '\u{ffff}',
                                None => return Err(Stop::Defer),
                            });
                        }
                        _ => return Err(Stop::Syntax),
                    }
                }
                c => out.push(c),
            }
        }
    }

    fn object(&mut self) -> Result<J, Stop> {
        self.at += 1;
        let mut dict = Vec::new();
        self.space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(J::Dict(dict));
        }
        loop {
            if self.peek() != Some(b'"') {
                return Err(Stop::Syntax);
            }
            let key = self.string()?;
            self.space();
            if self.peek() != Some(b':') {
                return Err(Stop::Syntax);
            }
            self.at += 1;
            self.space();
            let value = self.value()?;
            // dict assignment: a repeated key keeps its first place.
            set(&mut dict, &key, value);
            self.space();
            match self.peek() {
                Some(b',') => {
                    self.at += 1;
                    self.space();
                }
                Some(b'}') => {
                    self.at += 1;
                    return Ok(J::Dict(dict));
                }
                _ => return Err(Stop::Syntax),
            }
        }
    }

    fn array(&mut self) -> Result<J, Stop> {
        self.at += 1;
        let mut list = Vec::new();
        self.space();
        if self.peek() == Some(b']') {
            self.at += 1;
            return Ok(J::List(list));
        }
        loop {
            list.push(self.value()?);
            self.space();
            match self.peek() {
                Some(b',') => {
                    self.at += 1;
                    self.space();
                }
                Some(b']') => {
                    self.at += 1;
                    return Ok(J::List(list));
                }
                _ => return Err(Stop::Syntax),
            }
        }
    }
}

/// `json.loads(text)`: `Ok(None)` when Python raises a JSONDecodeError.
pub(super) fn loads(text: &str) -> Result<Option<J>, Defer> {
    read(text, false)
}

/// `loads`, reading a lone surrogate as U+FFFF (for a caller whose only
/// uses of strings treat both alike: never a principal name, never printable).
pub(super) fn loads_lossy(text: &str) -> Result<Option<J>, Defer> {
    read(text, true)
}

fn read(text: &str, lossy: bool) -> Result<Option<J>, Defer> {
    let mut reader = Reader {
        text,
        at: 0,
        depth: 0,
        lossy,
    };
    reader.space();
    let value = match reader.value() {
        Ok(value) => value,
        Err(Stop::Syntax) => return Ok(None),
        Err(Stop::Defer) => return Err(Defer),
    };
    reader.space();
    Ok((reader.at == text.len()).then_some(value))
}

/// Python `repr()` of a value `json.loads` produced.
pub(super) fn py_value_repr(value: &J) -> String {
    match value {
        J::Null => "None".to_owned(),
        J::Bool(true) => "True".to_owned(),
        J::Bool(false) => "False".to_owned(),
        J::Int(value) => value.clone(),
        J::Float(value) => float_repr(*value),
        J::Str(value) => super::super::mode_repr(value),
        J::List(values) => format!(
            "[{}]",
            values
                .iter()
                .map(py_value_repr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        J::Dict(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!(
                    "{}: {}",
                    super::super::mode_repr(key),
                    py_value_repr(value)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
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
        J::Float(value) if value.is_nan() => out.push_str("NaN"),
        J::Float(value) if value.is_infinite() => out.push_str(if *value > 0.0 {
            "Infinity"
        } else {
            "-Infinity"
        }),
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

/// `json.dumps(value)`: one line, `", "` and `": "` separators, ASCII.
pub(super) fn dumps_line(value: &J) -> String {
    let mut out = String::new();
    line(value, &mut out);
    out
}

fn line(value: &J, out: &mut String) {
    match value {
        J::List(values) => {
            out.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                line(value, out);
            }
            out.push(']');
        }
        J::Dict(values) => {
            out.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                quoted(key, true, out);
                out.push_str(": ");
                line(value, out);
            }
            out.push('}');
        }
        scalar => append(scalar, true, 0, out),
    }
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
    fn python_json_reader_domain() {
        // json.loads accepts NaN and the infinities; json.dumps writes them back.
        let value = loads("{\"a\": NaN, \"b\": -Infinity, \"c\": 1E2, \"d\": 1.50, \"e\": -0}")
            .unwrap()
            .unwrap();
        assert_eq!(
            dumps(&value, false),
            "{\n  \"a\": NaN,\n  \"b\": -Infinity,\n  \"c\": 100.0,\n  \"d\": 1.5,\n  \"e\": 0\n}"
        );
        // A lone surrogate is a Python str no Rust String can hold.
        assert!(loads("{\"a\": \"\\ud800\"}").is_err());
        assert!(loads("{\"a\": \"\\udc00\\ud800\"}").is_err());
        assert_eq!(
            loads("\"\\ud83d\\ude00\"").unwrap(),
            Some(J::Str("\u{1f600}".into()))
        );
        for refused in [
            "{\"a\": }",
            "[1,]",
            "{\"a\":1,}",
            "01",
            "-",
            "+1",
            "nan",
            "-NaN",
            "\"\\u12\"",
            "\"a\u{1}\"",
            "{\"a\" 1}",
            "\u{feff}{}",
            "{} x",
        ] {
            assert!(matches!(loads(refused), Ok(None)), "{refused}");
        }
        assert!(loads(&"[".repeat(150)).is_err());
    }

    #[test]
    fn container_str_is_python_repr() {
        let value = loads("{\"k\": [1, 2.5, null, true, \"it's\", {}], \"\u{e9}\": \"a\\nb\"}")
            .unwrap()
            .unwrap();
        assert_eq!(
            value.py_str(),
            "{'k': [1, 2.5, None, True, \"it's\", {}], '\u{e9}': 'a\\nb'}"
        );
        assert_eq!(J::Float(f64::NAN).py_str(), "nan");
        assert_eq!(J::Float(f64::NEG_INFINITY).py_str(), "-inf");
    }

    #[test]
    fn one_line_dump_matches_python_defaults() {
        let value = loads("{\"a\": [1, {\"b\": null}], \"c\": \"\u{e9}\", \"d\": {}}")
            .unwrap()
            .unwrap();
        assert_eq!(
            dumps_line(&value),
            "{\"a\": [1, {\"b\": null}], \"c\": \"\\u00e9\", \"d\": {}}"
        );
        assert_eq!(dumps_line(&J::Bool(false)), "false");
        assert_eq!(dumps_line(&J::Null), "null");
    }
}
