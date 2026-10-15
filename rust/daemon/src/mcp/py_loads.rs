//! Python's `json.loads` for the SDK's argument pre-parse
//! (`FuncMetadata.pre_parse_json`): JSON with Python's extensions
//! (`Infinity`, `-Infinity`), whitespace `[ \t\n\r]`, duplicate keys keeping
//! the first position and the last value, and the 4,300-digit int limit.
//!
//! Anything Python would refuse yields None, and the argument stays the
//! string. A string holding an unpaired surrogate escape has no Rust
//! `String`, so it yields None too (declared, `divergences.md`
//! MCP-BIND-PRE-PARSE). An infinity becomes an overflowing number literal
//! and NaN `binding::nan_number`, both read back by `binding::float_arg`.
//! Nesting deeper than `MAX_DEPTH` yields None where Python's limit depends
//! on its stack.

use serde_json::{Map, Number, Value};

const MAX_DEPTH: usize = 900;
const MAX_INT_DIGITS: usize = 4300;

pub fn loads(text: &str) -> Option<Value> {
    let mut p = Parser {
        text,
        s: text.as_bytes(),
        i: 0,
    };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    (p.i == p.s.len()).then_some(v)
}

struct Parser<'a> {
    text: &'a str,
    s: &'a [u8],
    /// Always on a character boundary of `text`: it only steps over ASCII
    /// bytes or whole characters.
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while matches!(self.s.get(self.i), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn eat(&mut self, lit: &str) -> bool {
        if self.s[self.i..].starts_with(lit.as_bytes()) {
            self.i += lit.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self, depth: usize) -> Option<Value> {
        if depth > MAX_DEPTH {
            return None;
        }
        match *self.s.get(self.i)? {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => self.string().map(Value::String),
            b'n' if self.eat("null") => Some(Value::Null),
            b't' if self.eat("true") => Some(Value::Bool(true)),
            b'f' if self.eat("false") => Some(Value::Bool(false)),
            b'N' if self.eat("NaN") => Some(Value::Number(super::binding::nan_number())),
            b'I' if crate::mutants::active("mcp-bind-no-infinity") => None,
            b'I' if self.eat("Infinity") => infinity(false),
            b'-' if self.s[self.i..].starts_with(b"-Infinity") => {
                self.i += "-Infinity".len();
                infinity(true)
            }
            b'-' | b'0'..=b'9' => self.number(),
            _ => None,
        }
    }

    fn object(&mut self, depth: usize) -> Option<Value> {
        self.i += 1;
        let mut map = Map::new();
        self.ws();
        if self.s.get(self.i) == Some(&b'}') {
            self.i += 1;
            return Some(Value::Object(map));
        }
        loop {
            self.ws();
            if self.s.get(self.i) != Some(&b'"') {
                return None;
            }
            let key = self.string()?;
            self.ws();
            if self.s.get(self.i) != Some(&b':') {
                return None;
            }
            self.i += 1;
            self.ws();
            let v = self.value(depth + 1)?;
            map.insert(key, v);
            self.ws();
            match self.s.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    return Some(Value::Object(map));
                }
                _ => return None,
            }
        }
    }

    fn array(&mut self, depth: usize) -> Option<Value> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.s.get(self.i) == Some(&b']') {
            self.i += 1;
            return Some(Value::Array(items));
        }
        loop {
            self.ws();
            items.push(self.value(depth + 1)?);
            self.ws();
            match self.s.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    return Some(Value::Array(items));
                }
                _ => return None,
            }
        }
    }

    /// `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?`: an int without a
    /// fraction or exponent (refused past 4,300 digits), else a float.
    fn number(&mut self) -> Option<Value> {
        let start = self.i;
        let digits = |p: &mut Self| {
            let from = p.i;
            while p.s.get(p.i).is_some_and(u8::is_ascii_digit) {
                p.i += 1;
            }
            p.i - from
        };
        if self.s.get(self.i) == Some(&b'-') {
            self.i += 1;
        }
        let int_start = self.i;
        match self.s.get(self.i) {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                digits(self);
            }
            _ => return None,
        }
        let int_digits = self.i - int_start;
        let mut float = false;
        // A fraction or exponent without digits ends the number before it,
        // as Python's NUMBER_RE does; the parse then fails on what follows.
        if self.s.get(self.i) == Some(&b'.')
            && self.s.get(self.i + 1).is_some_and(u8::is_ascii_digit)
        {
            self.i += 1;
            digits(self);
            float = true;
        }
        if matches!(self.s.get(self.i), Some(b'e' | b'E')) {
            let mark = self.i;
            self.i += 1;
            if matches!(self.s.get(self.i), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if digits(self) == 0 {
                self.i = mark;
            } else {
                float = true;
            }
        }
        if !float && int_digits > MAX_INT_DIGITS {
            return None;
        }
        self.text[start..self.i]
            .parse::<Number>()
            .ok()
            .map(Value::Number)
    }

    fn string(&mut self) -> Option<String> {
        self.i += 1;
        let mut out = String::new();
        loop {
            let b = *self.s.get(self.i)?;
            match b {
                b'"' => {
                    self.i += 1;
                    return Some(out);
                }
                b'\\' => {
                    self.i += 1;
                    let c = match *self.s.get(self.i)? {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => {
                            let hi = self.hex4()?;
                            if (0xD800..0xDC00).contains(&hi)
                                && self.s[self.i + 1..].starts_with(b"\\u")
                            {
                                self.i += 2;
                                let lo = self.hex4()?;
                                if (0xDC00..0xE000).contains(&lo) {
                                    char::from_u32(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))?
                                } else {
                                    // Python keeps the lone high surrogate.
                                    return None;
                                }
                            } else {
                                char::from_u32(hi)?
                            }
                        }
                        _ => return None,
                    };
                    out.push(c);
                    self.i += 1;
                }
                // strict=True: control characters are refused.
                0x00..=0x1f => return None,
                _ => {
                    let c = self.text[self.i..].chars().next()?;
                    out.push(c);
                    self.i += c.len_utf8();
                }
            }
        }
    }

    /// The four hex digits after `\u`; leaves `i` on the last digit.
    fn hex4(&mut self) -> Option<u32> {
        let digits = self.s.get(self.i + 1..self.i + 5)?;
        let text = std::str::from_utf8(digits).ok()?;
        if !text.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        self.i += 4;
        u32::from_str_radix(text, 16).ok()
    }
}

fn infinity(negative: bool) -> Option<Value> {
    let text = if negative { "-1e999" } else { "1e999" };
    text.parse::<Number>().ok().map(Value::Number)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn long_strings_parse_in_linear_time() {
        let text = format!("[\"{}\"]", "\u{e9}".repeat(1 << 20));
        let started = std::time::Instant::now();
        assert!(loads(&text).is_some());
        assert!(started.elapsed() < std::time::Duration::from_secs(2));
    }

    #[test]
    fn python_json_extensions() {
        assert_eq!(loads(" [1, \"a\", null] "), Some(json!([1, "a", null])));
        assert_eq!(
            super::super::binding::float_arg(&loads("[Infinity]").unwrap()[0]),
            Some(f64::INFINITY)
        );
        let nan = loads("[NaN]").unwrap();
        assert!(super::super::binding::float_arg(&nan[0]).is_some_and(f64::is_nan));
        assert_eq!(
            loads("{\"a\":1,\"b\":2,\"a\":3}"),
            Some(json!({"a": 3, "b": 2}))
        );
        assert_eq!(loads("[1,]"), None);
        assert_eq!(loads("\u{feff}[]"), None);
        assert_eq!(loads("\"\\ud83d\\ude00\""), Some(json!("\u{1f600}")));
        assert_eq!(loads("\"\\ud83d\""), None);
        assert_eq!(loads("[1.]"), None);
        assert_eq!(loads(&format!("[{}]", "1".repeat(4301))), None);
        assert!(loads(&format!("[{}]", "1".repeat(4300))).is_some());
    }
}
