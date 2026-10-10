//! Python `json`, `repr()` and `str()` semantics for the pairing commands'
//! daemon answers and reports.
//!
//! The reader follows CPython 3.11's C scanner over its whole domain:
//! `NaN`/`Infinity` literals, strict control characters, four-digit escapes,
//! a lone surrogate kept as the UTF-16 code unit Python holds (`J::Wtf`),
//! an integer past 4300 digits refused as `int()` refuses it (Python's
//! `ValueError`, which every caller here reads as "not JSON"), and nesting
//! up to the caller's measured recursion limit. Values keep Python's number
//! spelling (`int(lexeme)`, `repr(float(lexeme))`) and dict insertion order;
//! a duplicate key keeps its first position with its last value.
use std::fmt::Write as _;

/// A value `json.loads` produced.
#[derive(Clone, Debug, PartialEq)]
pub(super) enum J {
    Null,
    Bool(bool),
    Int(String),
    Float(f64),
    Str(String),
    /// A str holding at least one lone surrogate, as UTF-16 code units.
    Wtf(Vec<u16>),
    List(Vec<J>),
    /// Keys are `J::Str` or `J::Wtf`.
    Dict(Vec<(J, J)>),
}

pub(super) type Dict = Vec<(J, J)>;

impl From<&str> for J {
    fn from(text: &str) -> Self {
        J::Str(text.to_owned())
    }
}

/// `d.get(key)` with Python's `None` for both an absent key and JSON null.
pub(super) fn get<'a>(dict: &'a Dict, key: &str) -> Option<&'a J> {
    match dict
        .iter()
        .find(|(k, _)| k.as_str() == Some(key))
        .map(|(_, v)| v)
    {
        Some(J::Null) | None => None,
        Some(value) => Some(value),
    }
}

/// `d[key] = value`: an existing key keeps its position.
pub(super) fn set(dict: &mut Dict, key: &str, value: J) {
    set_key(dict, J::Str(key.to_owned()), value);
}

fn set_key(dict: &mut Dict, key: J, value: J) {
    match dict.iter_mut().find(|(k, _)| *k == key) {
        Some(slot) => slot.1 = value,
        None => dict.push((key, value)),
    }
}

/// A str from UTF-16 code units: `J::Str` when they are valid, else `J::Wtf`.
pub(super) fn text(units: Vec<u16>) -> J {
    match String::from_utf16(&units) {
        Ok(text) => J::Str(text),
        Err(_) => J::Wtf(units),
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
            J::Wtf(units) => !units.is_empty(),
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

    /// `str(value)` as UTF-16 code units: a str as itself, anything else
    /// its repr.
    pub(super) fn py_str_units(&self) -> Vec<u16> {
        match self {
            J::Str(value) => value.encode_utf16().collect(),
            J::Wtf(units) => units.clone(),
            other => py_value_repr(other).encode_utf16().collect(),
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

/// The deepest container nesting (the outermost included) each oracle read
/// path parses before CPython raises `RecursionError`. Measured 2026-10-09
/// on CPython 3.11.9 through the oracle CLI itself (bisection of `[`-nested
/// values in the answer; the printing of such values never failed first).
pub(super) const PAIR_DEPTH: usize = 987;
pub(super) const INVITE_DEPTH: usize = 985;
// CPython 3.11's int_max_str_digits default.
const MAX_INT_DIGITS: usize = 4300;

/// How `json.loads` ended without a value.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Refused {
    /// `JSONDecodeError`, or `int()`'s digit-limit `ValueError`.
    NotJson,
    /// `RecursionError`: nesting past the caller's limit.
    TooDeep,
}

/// CPython's C scanner (`json.loads` with its defaults).
struct Reader<'a> {
    text: &'a str,
    at: usize,
    depth: usize,
    limit: usize,
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

    fn value(&mut self) -> Result<J, Refused> {
        match self.peek() {
            Some(b'"') => self.string().map(text),
            Some(b'{') => self.nested(Self::object),
            Some(b'[') => self.nested(Self::array),
            Some(b'n') if self.word("null") => Ok(J::Null),
            Some(b't') if self.word("true") => Ok(J::Bool(true)),
            Some(b'f') if self.word("false") => Ok(J::Bool(false)),
            Some(b'N') if self.word("NaN") => Ok(J::Float(f64::NAN)),
            Some(b'I') if self.word("Infinity") => Ok(J::Float(f64::INFINITY)),
            Some(b'-') if self.word("-Infinity") => Ok(J::Float(f64::NEG_INFINITY)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(Refused::NotJson),
        }
    }

    fn nested(&mut self, read: fn(&mut Self) -> Result<J, Refused>) -> Result<J, Refused> {
        self.depth += 1;
        if self.depth > self.limit {
            return Err(Refused::TooDeep);
        }
        // Nearly a thousand levels outgrow a debug build's main-thread stack.
        let value = stacker::maybe_grow(64 * 1024, 1024 * 1024, || read(self));
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
    fn number(&mut self) -> Result<J, Refused> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        match self.peek() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return Err(Refused::NotJson),
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
            // float() of any such lexeme is a value (huge ones are inf).
            return lexeme
                .parse::<f64>()
                .map(J::Float)
                .map_err(|_| Refused::NotJson);
        }
        let digits = &self.text[start..integer_end];
        let magnitude = digits.strip_prefix('-').unwrap_or(digits);
        if magnitude.len() > MAX_INT_DIGITS {
            return Err(Refused::NotJson);
        }
        Ok(J::Int(if magnitude == "0" {
            "0".to_owned()
        } else {
            digits.to_owned()
        }))
    }

    fn hex4(&mut self) -> Result<u16, Refused> {
        let digits = self
            .text
            .get(self.at..self.at + 4)
            .ok_or(Refused::NotJson)?;
        if !digits.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(Refused::NotJson);
        }
        self.at += 4;
        u16::from_str_radix(digits, 16).map_err(|_| Refused::NotJson)
    }

    /// A string's UTF-16 code units (Python keeps a lone surrogate as is).
    fn string(&mut self) -> Result<Vec<u16>, Refused> {
        self.at += 1;
        let mut out = Vec::new();
        loop {
            let rest = &self.text[self.at..];
            let c = rest.chars().next().ok_or(Refused::NotJson)?;
            self.at += c.len_utf8();
            match c {
                '"' => return Ok(out),
                '\u{0}'..='\u{1f}' => return Err(Refused::NotJson),
                '\\' => {
                    let escape = self.peek().ok_or(Refused::NotJson)?;
                    self.at += 1;
                    let unit = match escape {
                        b'"' => u16::from(b'"'),
                        b'\\' => u16::from(b'\\'),
                        b'/' => u16::from(b'/'),
                        b'b' => 0x8,
                        b'f' => 0xc,
                        b'n' => u16::from(b'\n'),
                        b'r' => u16::from(b'\r'),
                        b't' => u16::from(b'\t'),
                        b'u' => {
                            let unit = self.hex4()?;
                            if (0xd800..0xdc00).contains(&unit)
                                && self.text[self.at..].starts_with("\\u")
                            {
                                let mark = self.at;
                                self.at += 2;
                                let low = self.hex4()?;
                                if (0xdc00..0xe000).contains(&low) {
                                    out.push(unit);
                                    low
                                } else {
                                    self.at = mark;
                                    unit
                                }
                            } else {
                                unit
                            }
                        }
                        _ => return Err(Refused::NotJson),
                    };
                    out.push(unit);
                }
                c => {
                    let mut buffer = [0u16; 2];
                    out.extend_from_slice(c.encode_utf16(&mut buffer));
                }
            }
        }
    }

    fn object(&mut self) -> Result<J, Refused> {
        self.at += 1;
        let mut dict = Vec::new();
        self.space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            return Ok(J::Dict(dict));
        }
        loop {
            if self.peek() != Some(b'"') {
                return Err(Refused::NotJson);
            }
            let key = text(self.string()?);
            self.space();
            if self.peek() != Some(b':') {
                return Err(Refused::NotJson);
            }
            self.at += 1;
            self.space();
            let value = self.value()?;
            // dict assignment: a repeated key keeps its first place.
            set_key(&mut dict, key, value);
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
                _ => return Err(Refused::NotJson),
            }
        }
    }

    fn array(&mut self) -> Result<J, Refused> {
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
                _ => return Err(Refused::NotJson),
            }
        }
    }
}

/// `json.loads(text)` on a path whose recursion limit is `limit`.
pub(super) fn loads(text: &str, limit: usize) -> Result<J, Refused> {
    let mut reader = Reader {
        text,
        at: 0,
        depth: 0,
        limit,
    };
    reader.space();
    let value = reader.value()?;
    reader.space();
    if reader.at == text.len() {
        Ok(value)
    } else {
        Err(Refused::NotJson)
    }
}

/// Python's `repr()` of a str (`unicode_repr`), from its code units.
fn str_repr(units: &[u16], out: &mut String) {
    let chars: Vec<Result<char, u16>> = char::decode_utf16(units.iter().copied())
        .map(|item| item.map_err(|error| error.unpaired_surrogate()))
        .collect();
    let has = |wanted: char| chars.contains(&Ok(wanted));
    let quote = if has('\'') && !has('"') { '"' } else { '\'' };
    out.push(quote);
    for item in chars {
        match item {
            Err(unit) => {
                let _ = write!(out, "\\u{unit:04x}");
            }
            Ok('\\') => out.push_str("\\\\"),
            Ok('\t') => out.push_str("\\t"),
            Ok('\n') => out.push_str("\\n"),
            Ok('\r') => out.push_str("\\r"),
            Ok(c) if c == quote => {
                out.push('\\');
                out.push(c);
            }
            Ok(c) if !super::printable(c) => {
                let point = c as u32;
                if point <= 0xff {
                    let _ = write!(out, "\\x{point:02x}");
                } else if point <= 0xffff {
                    let _ = write!(out, "\\u{point:04x}");
                } else {
                    let _ = write!(out, "\\U{point:08x}");
                }
            }
            Ok(c) => out.push(c),
        }
    }
    out.push(quote);
}

/// Python `repr()` of a value `json.loads` produced.
pub(super) fn py_value_repr(value: &J) -> String {
    let mut out = String::new();
    repr_into(value, &mut out);
    out
}

fn repr_into(value: &J, out: &mut String) {
    match value {
        J::Null => out.push_str("None"),
        J::Bool(true) => out.push_str("True"),
        J::Bool(false) => out.push_str("False"),
        J::Int(value) => out.push_str(value),
        J::Float(value) => out.push_str(&float_repr(*value)),
        J::Str(value) => str_repr(&value.encode_utf16().collect::<Vec<_>>(), out),
        J::Wtf(units) => str_repr(units, out),
        J::List(values) => {
            out.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                stacker::maybe_grow(64 * 1024, 1024 * 1024, || repr_into(value, out));
            }
            out.push(']');
        }
        J::Dict(values) => {
            out.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                repr_into(key, out);
                out.push_str(": ");
                stacker::maybe_grow(64 * 1024, 1024 * 1024, || repr_into(value, out));
            }
            out.push('}');
        }
    }
}

/// Python float repr (`float_repr_style == 'short'`): the shortest digit
/// string that reads back as `value` and, among those, the one nearest to
/// it, an exact tie going to the even digit (David Gay's mode 0). Rust's
/// shortest formatting fixes the digit count; the digits themselves are
/// the correctly rounded (half to even) prefix of the exact binary value
/// whenever that prefix reads back, else Rust's own.
pub(super) fn float_repr(value: f64) -> String {
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
    let magnitude = value.abs();
    let (digits, exponent) = shortest_digits(magnitude);
    let mut out = String::new();
    if value.is_sign_negative() {
        out.push('-');
    }
    if !(-4..16).contains(&exponent) {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        let _ = write!(out, "e{sign}{:02}", exponent.abs());
    } else {
        let point = exponent + 1;
        if point <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-point) as usize));
            out.push_str(&digits);
        } else if point as usize >= digits.len() {
            out.push_str(&digits);
            out.extend(std::iter::repeat_n('0', point as usize - digits.len()));
            out.push_str(".0");
        } else {
            let point = point as usize;
            out.push_str(&digits[..point]);
            out.push('.');
            out.push_str(&digits[point..]);
        }
    }
    out
}

/// Mantissa and decimal exponent of `format!("{:e}")`.
fn scientific(text: &str) -> (String, i32) {
    let (mantissa, power) = text.split_once('e').unwrap_or((text, "0"));
    (
        mantissa.chars().filter(|c| *c != '.').collect(),
        power.parse().unwrap_or(0),
    )
}

/// The significant digits (no trailing zeros) and decimal exponent of
/// CPython's repr of a positive finite `value`.
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

fn quoted(units: &[u16], ascii: bool, out: &mut String) {
    out.push('"');
    for item in char::decode_utf16(units.iter().copied()) {
        match item {
            Err(error) => {
                let _ = write!(out, "\\u{:04x}", error.unpaired_surrogate());
            }
            Ok('"') => out.push_str("\\\""),
            Ok('\\') => out.push_str("\\\\"),
            Ok('\n') => out.push_str("\\n"),
            Ok('\r') => out.push_str("\\r"),
            Ok('\t') => out.push_str("\\t"),
            Ok('\u{8}') => out.push_str("\\b"),
            Ok('\u{c}') => out.push_str("\\f"),
            Ok(value @ '\u{0}'..='\u{1f}') => {
                let _ = write!(out, "\\u{:04x}", value as u32);
            }
            Ok(value @ '\u{20}'..='\u{7e}') => out.push(value),
            Ok(value) if !ascii => out.push(value),
            Ok(value) => {
                let mut buffer = [0u16; 2];
                for unit in value.encode_utf16(&mut buffer) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

fn string_units(value: &J) -> Option<Vec<u16>> {
    match value {
        J::Str(text) => Some(text.encode_utf16().collect()),
        J::Wtf(units) => Some(units.clone()),
        _ => None,
    }
}

fn scalar(value: &J, ascii: bool, out: &mut String) {
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
        other => quoted(&string_units(other).unwrap_or_default(), ascii, out),
    }
}

fn append(value: &J, ascii: bool, level: usize, out: &mut String) {
    let pad = |out: &mut String, level: usize| {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', level * 2));
    };
    match value {
        J::List(values) if values.is_empty() => out.push_str("[]"),
        J::List(values) => {
            out.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                pad(out, level + 1);
                stacker::maybe_grow(64 * 1024, 1024 * 1024, || {
                    append(value, ascii, level + 1, out);
                });
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
                scalar(key, ascii, out);
                out.push_str(": ");
                stacker::maybe_grow(64 * 1024, 1024 * 1024, || {
                    append(value, ascii, level + 1, out);
                });
            }
            pad(out, level);
            out.push('}');
        }
        other => scalar(other, ascii, out),
    }
}

/// `json.dumps(value, indent=2, ensure_ascii=ascii)`.
pub(super) fn dumps(value: &J, ascii: bool) -> String {
    let mut out = String::new();
    stacker::maybe_grow(64 * 1024, 1024 * 1024, || append(value, ascii, 0, &mut out));
    out
}

/// `json.dumps(value)`: one line, `", "` and `": "` separators, ASCII.
pub(super) fn dumps_line(value: &J) -> String {
    let mut out = String::new();
    stacker::maybe_grow(64 * 1024, 1024 * 1024, || line(value, &mut out));
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
                stacker::maybe_grow(64 * 1024, 1024 * 1024, || line(value, out));
            }
            out.push(']');
        }
        J::Dict(values) => {
            out.push('{');
            for (index, (key, value)) in values.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                scalar(key, true, out);
                out.push_str(": ");
                stacker::maybe_grow(64 * 1024, 1024 * 1024, || line(value, out));
            }
            out.push('}');
        }
        other => scalar(other, true, out),
    }
}

/// UTF-16 code units as a text stream with `errors="backslashreplace"`
/// writes them (Python's stderr): a lone surrogate becomes `\udXXX`.
pub(super) fn backslashreplace(units: &[u16]) -> String {
    let mut out = String::new();
    for item in char::decode_utf16(units.iter().copied()) {
        match item {
            Ok(c) => out.push(c),
            Err(error) => {
                let _ = write!(out, "\\u{:04x}", error.unpaired_surrogate());
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(text: &str) -> J {
        loads(text, PAIR_DEPTH).unwrap()
    }

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
            // 2^49 + 0.25: an exact tie at sixteen digits.
            (562_949_953_421_312.0 + 0.25, "562949953421312.2"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
        ] {
            assert_eq!(float_repr(value), expected);
        }
    }

    /// CPython 3.11's repr of binary64 values with exact decimal ties at
    /// the shortest length, plus master's random and boundary table.
    #[test]
    fn float_repr_matches_cpython_tables() {
        for table in [
            include_str!("float_repr_ties.tsv"),
            include_str!("../../../tests/cli_audit_float_repr.tsv"),
        ] {
            for line in table
                .lines()
                .filter(|l| !l.starts_with('#') && !l.is_empty())
            {
                let (bits, expected) = line.split_once('\t').expect("bits TAB repr");
                let value = f64::from_bits(u64::from_str_radix(bits, 16).unwrap());
                assert_eq!(float_repr(value), expected, "bits {bits}");
            }
        }
    }

    #[test]
    fn pretty_dump_matches_python_layout() {
        let value = read(r#"{"a": [1, 2.50, {}], "b": {"c": "é\u0001"}, "d": [], "a": -0}"#);
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
        let value = read("{\"a\": NaN, \"b\": -Infinity, \"c\": 1E2, \"d\": 1.50, \"e\": -0}");
        assert_eq!(
            dumps(&value, false),
            "{\n  \"a\": NaN,\n  \"b\": -Infinity,\n  \"c\": 100.0,\n  \"d\": 1.5,\n  \"e\": 0\n}"
        );
        assert_eq!(read("\"\\ud83d\\ude00\""), J::Str("\u{1f600}".into()));
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
            assert_eq!(
                loads(refused, PAIR_DEPTH),
                Err(Refused::NotJson),
                "{refused}"
            );
        }
    }

    #[test]
    fn lone_surrogates_are_kept_as_python_holds_them() {
        let value = read("{\"a\": \"x\\ud800\", \"\\udc00\": [\"\\udc00\\ud800\"]}");
        assert_eq!(
            dumps_line(&value),
            "{\"a\": \"x\\ud800\", \"\\udc00\": [\"\\udc00\\ud800\"]}"
        );
        assert_eq!(
            py_value_repr(&value),
            "{'a': 'x\\ud800', '\\udc00': ['\\udc00\\ud800']}"
        );
        let J::Dict(dict) = value else { panic!() };
        assert_eq!(get(&dict, "a"), Some(&J::Wtf(vec![0x78, 0xd800])));
        assert_eq!(backslashreplace(&[0x61, 0xd800, 0x62]), "a\\ud800b");
    }

    #[test]
    fn integers_past_the_digit_limit_are_python_value_errors() {
        let limit = "9".repeat(4300);
        assert_eq!(
            read(&format!("[-{limit}]")),
            J::List(vec![J::Int(format!("-{limit}"))])
        );
        assert_eq!(
            loads(&format!("[{limit}9]"), PAIR_DEPTH),
            Err(Refused::NotJson)
        );
        assert!(matches!(read(&format!("[{limit}9.5]")), J::List(_)));
    }

    #[test]
    fn nesting_stops_at_the_measured_recursion_limit() {
        let nest = |depth: usize| format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        assert!(loads(&nest(PAIR_DEPTH), PAIR_DEPTH).is_ok());
        assert_eq!(
            loads(&nest(PAIR_DEPTH + 1), PAIR_DEPTH),
            Err(Refused::TooDeep)
        );
        assert!(loads(&nest(INVITE_DEPTH), INVITE_DEPTH).is_ok());
        assert_eq!(
            loads(&nest(INVITE_DEPTH + 1), INVITE_DEPTH),
            Err(Refused::TooDeep)
        );
        let deep = loads(&nest(PAIR_DEPTH), PAIR_DEPTH).unwrap();
        assert_eq!(dumps_line(&deep).len(), 2 * PAIR_DEPTH);
        assert_eq!(py_value_repr(&deep).len(), 2 * PAIR_DEPTH);
    }

    #[test]
    fn container_str_is_python_repr() {
        let value = read("{\"k\": [1, 2.5, null, true, \"it's\", {}], \"\u{e9}\": \"a\\nb\"}");
        assert_eq!(
            String::from_utf16(&value.py_str_units()).unwrap(),
            "{'k': [1, 2.5, None, True, \"it's\", {}], '\u{e9}': 'a\\nb'}"
        );
        assert_eq!(py_value_repr(&J::Float(f64::NAN)), "nan");
        assert_eq!(py_value_repr(&J::Float(f64::NEG_INFINITY)), "-inf");
    }

    #[test]
    fn one_line_dump_matches_python_defaults() {
        let value = read("{\"a\": [1, {\"b\": null}], \"c\": \"\u{e9}\", \"d\": {}}");
        assert_eq!(
            dumps_line(&value),
            "{\"a\": [1, {\"b\": null}], \"c\": \"\\u00e9\", \"d\": {}}"
        );
        assert_eq!(dumps_line(&J::Bool(false)), "false");
        assert_eq!(dumps_line(&J::Null), "null");
    }
}
