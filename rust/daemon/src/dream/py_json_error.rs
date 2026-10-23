//! The message of the exception CPython 3.11's `json.loads` raises for a
//! document it refuses: `str(JSONDecodeError)` (`"<msg>: line L column C
//! (char P)"`, positions in code points), the integer digit limit's
//! `ValueError`, or the scanner's `RecursionError`. It follows the C
//! scanner (`Modules/_json.c`: `scan_once_unicode`, `_parse_object_unicode`,
//! `_parse_array_unicode`, `scanstring_unicode`, `_match_number_unicode`)
//! and `json.decoder.JSONDecoder.decode`, the first failure left to right
//! deciding.
//!
//! The dream's lesson stage reports `str(exc)` of a failed extraction in
//! its answer, so the text is part of the contract.

/// CPython's `int()` digit limit (`sys.int_info.default_max_str_digits`).
const MAX_INT_DIGITS: usize = 4300;
/// Nesting at which the daemon's parser refuses a reply (`py_loads`), the
/// stand-in for Python's recursion limit.
const MAX_NESTING: usize = 900;

enum Fail {
    /// `StopIteration(idx)`: no value starts here.
    NoValue(usize),
    /// `JSONDecodeError(msg, doc, pos)`.
    Decode(&'static str, usize),
    /// Any other `ValueError` / `RecursionError`: its whole message.
    Other(String),
}

struct Scanner<'a> {
    s: &'a [char],
}

fn ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r')
}

impl Scanner<'_> {
    fn at(&self, i: usize) -> Option<char> {
        self.s.get(i).copied()
    }

    fn skip_ws(&self, mut i: usize) -> usize {
        while self.at(i).is_some_and(ws) {
            i += 1;
        }
        i
    }

    fn lit(&self, i: usize, word: &str) -> bool {
        word.chars()
            .enumerate()
            .all(|(k, c)| self.at(i + k) == Some(c))
    }

    fn scan_once(&self, idx: usize, depth: usize) -> Result<usize, Fail> {
        let Some(c) = self.at(idx) else {
            return Err(Fail::NoValue(idx));
        };
        let len = self.s.len();
        match c {
            '"' => self.string(idx + 1),
            '{' | '[' if depth >= MAX_NESTING => Err(Fail::Other(format!(
                "maximum recursion depth exceeded while decoding a JSON {} from a unicode string",
                if c == '{' { "object" } else { "array" }
            ))),
            '{' => self.object(idx + 1, depth + 1),
            '[' => self.array(idx + 1, depth + 1),
            'n' if idx + 3 < len && self.lit(idx, "null") => Ok(idx + 4),
            't' if idx + 3 < len && self.lit(idx, "true") => Ok(idx + 4),
            'f' if idx + 4 < len && self.lit(idx, "false") => Ok(idx + 5),
            'N' if idx + 2 < len && self.lit(idx, "NaN") => Ok(idx + 3),
            'I' if idx + 7 < len && self.lit(idx, "Infinity") => Ok(idx + 8),
            '-' if idx + 8 < len && self.lit(idx, "-Infinity") => Ok(idx + 9),
            _ => self.number(idx),
        }
    }

    fn number(&self, start: usize) -> Result<usize, Fail> {
        let digit = |i: usize| self.at(i).is_some_and(|c| c.is_ascii_digit());
        let mut idx = start;
        if self.at(idx) == Some('-') {
            idx += 1;
            if idx >= self.s.len() {
                return Err(Fail::NoValue(start));
            }
        }
        let int_start = idx;
        match self.at(idx) {
            Some('1'..='9') => {
                idx += 1;
                while digit(idx) {
                    idx += 1;
                }
            }
            Some('0') => idx += 1,
            _ => return Err(Fail::NoValue(start)),
        }
        let int_end = idx;
        let mut float = false;
        if self.at(idx) == Some('.') && digit(idx + 1) {
            float = true;
            idx += 2;
            while digit(idx) {
                idx += 1;
            }
        }
        if matches!(self.at(idx), Some('e' | 'E')) && idx + 1 < self.s.len() {
            let e_start = idx;
            idx += 1;
            if matches!(self.at(idx), Some('-' | '+')) && idx + 1 < self.s.len() {
                idx += 1;
            }
            while digit(idx) {
                idx += 1;
            }
            if digit(idx - 1) {
                float = true;
            } else {
                idx = e_start;
            }
        }
        let digits = int_end - int_start;
        if !float && digits > MAX_INT_DIGITS {
            return Err(Fail::Other(format!(
                "Exceeds the limit ({MAX_INT_DIGITS} digits) for integer string conversion: \
                 value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
            )));
        }
        Ok(idx)
    }

    /// `scanstring_unicode(pystr, end, strict=True)`: `end` is past the
    /// opening quote; returns the index past the closing one.
    fn string(&self, mut end: usize) -> Result<usize, Fail> {
        let begin = end - 1;
        let len = self.s.len();
        let hex = |c: Option<char>| c.and_then(|c| c.to_digit(16));
        loop {
            let mut next = end;
            let mut c = '\0';
            while next < len {
                c = self.s[next];
                if c == '"' || c == '\\' {
                    break;
                }
                if (c as u32) <= 0x1f {
                    return Err(Fail::Decode("Invalid control character at", next));
                }
                next += 1;
            }
            if next >= len || (c != '"' && c != '\\') {
                return Err(Fail::Decode("Unterminated string starting at", begin));
            }
            next += 1;
            if c == '"' {
                return Ok(next);
            }
            if next == len {
                return Err(Fail::Decode("Unterminated string starting at", begin));
            }
            let e = self.s[next];
            if e != 'u' {
                end = next + 1;
                if !matches!(e, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't') {
                    return Err(Fail::Decode("Invalid \\escape", end - 2));
                }
                continue;
            }
            next += 1;
            end = next + 4;
            if end >= len {
                return Err(Fail::Decode("Invalid \\uXXXX escape", next - 1));
            }
            let mut code = 0u32;
            for k in next..end {
                match hex(self.at(k)) {
                    Some(d) => code = (code << 4) | d,
                    None => return Err(Fail::Decode("Invalid \\uXXXX escape", end - 5)),
                }
            }
            next = end;
            if (0xD800..=0xDBFF).contains(&code)
                && end + 6 < len
                && self.at(next) == Some('\\')
                && self.at(next + 1) == Some('u')
            {
                next += 2;
                end += 6;
                let mut low = 0u32;
                for k in next..end {
                    match hex(self.at(k)) {
                        Some(d) => low = (low << 4) | d,
                        None => return Err(Fail::Decode("Invalid \\uXXXX escape", end - 5)),
                    }
                }
                if !(0xDC00..=0xDFFF).contains(&low) {
                    end -= 6;
                }
            }
        }
    }

    fn object(&self, idx: usize, depth: usize) -> Result<usize, Fail> {
        let mut idx = self.skip_ws(idx);
        if self.at(idx) == Some('}') {
            return Ok(idx + 1);
        }
        loop {
            if self.at(idx) != Some('"') {
                return Err(Fail::Decode(
                    "Expecting property name enclosed in double quotes",
                    idx,
                ));
            }
            idx = self.skip_ws(self.string(idx + 1)?);
            if self.at(idx) != Some(':') {
                return Err(Fail::Decode("Expecting ':' delimiter", idx));
            }
            idx = self.skip_ws(idx + 1);
            idx = self.skip_ws(self.scan_once(idx, depth)?);
            if self.at(idx) == Some('}') {
                return Ok(idx + 1);
            }
            if self.at(idx) != Some(',') {
                return Err(Fail::Decode("Expecting ',' delimiter", idx));
            }
            idx = self.skip_ws(idx + 1);
        }
    }

    fn array(&self, idx: usize, depth: usize) -> Result<usize, Fail> {
        let mut idx = self.skip_ws(idx);
        if self.at(idx) == Some(']') {
            return Ok(idx + 1);
        }
        loop {
            idx = self.skip_ws(self.scan_once(idx, depth)?);
            if self.at(idx) == Some(']') {
                return Ok(idx + 1);
            }
            if self.at(idx) != Some(',') {
                return Err(Fail::Decode("Expecting ',' delimiter", idx));
            }
            idx = self.skip_ws(idx + 1);
        }
    }
}

/// `JSONDecodeError`'s message: `"<msg>: line L column C (char P)"`.
fn located(msg: &str, s: &[char], pos: usize) -> String {
    let before = &s[..pos.min(s.len())];
    let line = before.iter().filter(|&&c| c == '\n').count() + 1;
    let col = match before.iter().rposition(|&c| c == '\n') {
        Some(nl) => pos - nl,
        None => pos + 1,
    };
    format!("{msg}: line {line} column {col} (char {pos})")
}

/// `str(exc)` of what `json.loads(text)` raises, or None when it parses.
pub fn message(text: &str) -> Option<String> {
    let s: Vec<char> = text.chars().collect();
    if s.first() == Some(&'\u{feff}') {
        return Some(located(
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            &s,
            0,
        ));
    }
    let sc = Scanner { s: &s };
    let start = sc.skip_ws(0);
    let fail = match sc.scan_once(start, 0) {
        Ok(end) => {
            let end = sc.skip_ws(end);
            if end == s.len() {
                return None;
            }
            Fail::Decode("Extra data", end)
        }
        Err(f) => f,
    };
    Some(match fail {
        Fail::NoValue(pos) => located("Expecting value", &s, pos),
        Fail::Decode(msg, pos) => located(msg, &s, pos),
        Fail::Other(text) => text,
    })
}

#[cfg(test)]
mod tests {
    /// Expected values: CPython 3.11's `str(exc)` for `json.loads(case)`.
    #[test]
    fn messages_match_cpython() {
        let bs = '\\';
        let cases: Vec<(String, Option<&str>)> = vec![
            (
                "{bad".into(),
                Some("Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
            ),
            (
                "{\"a\" 1}".into(),
                Some("Expecting ':' delimiter: line 1 column 6 (char 5)"),
            ),
            (
                "{\"a\":1,}".into(),
                Some("Expecting property name enclosed in double quotes: line 1 column 8 (char 7)"),
            ),
            (
                "[1,]".into(),
                Some("Expecting value: line 1 column 4 (char 3)"),
            ),
            (
                "[1 2]".into(),
                Some("Expecting ',' delimiter: line 1 column 4 (char 3)"),
            ),
            (
                "\"abc".into(),
                Some("Unterminated string starting at: line 1 column 1 (char 0)"),
            ),
            (
                "\"a\u{1}\"".into(),
                Some("Invalid control character at: line 1 column 3 (char 2)"),
            ),
            (
                format!("\"{bs}q\""),
                Some("Invalid \\escape: line 1 column 2 (char 1)"),
            ),
            (
                format!("\"{bs}u12x4\""),
                Some("Invalid \\uXXXX escape: line 1 column 3 (char 2)"),
            ),
            ("01".into(), Some("Extra data: line 1 column 2 (char 1)")),
            (
                "{\"a\":}".into(),
                Some("Expecting value: line 1 column 6 (char 5)"),
            ),
            (
                "\u{feff}{}".into(),
                Some("Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)"),
            ),
            (
                "\n\n  {\"a\":tru}".into(),
                Some("Expecting value: line 3 column 8 (char 9)"),
            ),
            (
                format!("[\"{bs}ud800{bs}u12\"]"),
                Some("Invalid \\uXXXX escape: line 1 column 10 (char 9)"),
            ),
            ("1e".into(), Some("Extra data: line 1 column 2 (char 1)")),
            (
                "-".into(),
                Some("Expecting value: line 1 column 1 (char 0)"),
            ),
            (
                format!("\"{bs}ud800{bs}uzzzz\""),
                Some("Invalid \\uXXXX escape: line 1 column 9 (char 8)"),
            ),
            (
                "   ".into(),
                Some("Expecting value: line 1 column 4 (char 3)"),
            ),
            (
                "{\"\u{e9}\":\"\u{fc}\" x}".into(),
                Some("Expecting ',' delimiter: line 1 column 10 (char 9)"),
            ),
            (
                "-Infinityx".into(),
                Some("Extra data: line 1 column 10 (char 9)"),
            ),
            (
                "[-]".into(),
                Some("Expecting value: line 1 column 2 (char 1)"),
            ),
            ("1.e5".into(), Some("Extra data: line 1 column 2 (char 1)")),
            (
                format!("\"{bs}"),
                Some("Unterminated string starting at: line 1 column 1 (char 0)"),
            ),
            (
                format!("\"{bs}u1234"),
                Some("Invalid \\uXXXX escape: line 1 column 3 (char 2)"),
            ),
            (
                format!("\"{bs}ud800{bs}"),
                Some("Unterminated string starting at: line 1 column 1 (char 0)"),
            ),
            (
                format!("\"{bs}ud800{bs}u1234"),
                Some("Invalid \\uXXXX escape: line 1 column 9 (char 8)"),
            ),
            (
                "\t[\r\n1,\n 2 3]".into(),
                Some("Expecting ',' delimiter: line 3 column 4 (char 10)"),
            ),
            (
                "1".repeat(4301),
                Some(
                    "Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit",
                ),
            ),
            (
                format!("-{}", "1".repeat(4400)),
                Some(
                    "Exceeds the limit (4300 digits) for integer string conversion: value has 4400 digits; use sys.set_int_max_str_digits() to increase the limit",
                ),
            ),
            (format!("{}.5", "1".repeat(4300)), None),
            (
                "{\"a\": [1, NaN, -Infinity, null, true, false, 1e-3, \"\\u00e9\"]}".into(),
                None,
            ),
        ];
        for (case, want) in cases {
            assert_eq!(super::message(&case).as_deref(), want, "{case:?}");
        }
    }
}
