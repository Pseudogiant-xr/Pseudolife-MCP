//! The lease peer uses Python's JSON domain, including surrogate codepoints and
//! non-finite numbers. Keep these values intact rather than losing a reply.
use std::{
    fmt,
    ops::{Index, IndexMut},
};

#[derive(Clone, Debug, PartialEq)]
pub struct Text {
    points: Vec<u32>,
    valid: Option<String>,
}
impl Text {
    fn new(points: Vec<u32>) -> Self {
        let valid = points.iter().copied().map(char::from_u32).collect();
        Self { points, valid }
    }
    pub fn is_empty(&self) -> bool {
        self.points.is_empty()
    }
    pub fn clean(&self) -> String {
        self.points
            .iter()
            .copied()
            .map(|c| char::from_u32(c).unwrap_or('?'))
            .collect()
    }
    fn quoted(&self) -> String {
        let mut result = String::from("\"");
        for point in &self.points {
            match *point {
                34 => result.push_str("\\\""),
                92 => result.push_str("\\\\"),
                8 => result.push_str("\\b"),
                12 => result.push_str("\\f"),
                10 => result.push_str("\\n"),
                13 => result.push_str("\\r"),
                9 => result.push_str("\\t"),
                32..=126 => result.push(char::from_u32(*point).unwrap()),
                0..=65535 => {
                    use fmt::Write;
                    let _ = write!(result, "\\u{point:04x}");
                }
                _ => {
                    use fmt::Write;
                    let mut units = [0; 2];
                    for unit in char::from_u32(*point).unwrap().encode_utf16(&mut units) {
                        let _ = write!(result, "\\u{unit:04x}");
                    }
                }
            }
        }
        result.push('"');
        result
    }
    fn repr(&self) -> String {
        if let Some(valid) = &self.valid {
            return super::repr(valid);
        }
        let quote = if self.points.contains(&39) && !self.points.contains(&34) {
            '"'
        } else {
            '\''
        };
        let mut result = quote.to_string();
        for point in &self.points {
            match char::from_u32(*point) {
                None => {
                    use fmt::Write;
                    let _ = write!(result, "\\u{point:04x}");
                }
                Some(c) if c == quote => {
                    result.push('\\');
                    result.push(c);
                }
                Some(c) if c == '\'' || c == '"' => result.push(c),
                Some(c) => {
                    let one = super::repr(&c.to_string());
                    result.push_str(&one[1..one.len() - 1]);
                }
            }
        }
        result.push(quote);
        result
    }
}
impl From<&str> for Text {
    fn from(v: &str) -> Self {
        Self::new(v.chars().map(u32::from).collect())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Number {
    Ordinary(serde_json::Number),
    Nan,
    PositiveInfinity,
    NegativeInfinity,
}
impl Number {
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Self::Ordinary(n) => n.as_f64(),
            Self::Nan => Some(f64::NAN),
            Self::PositiveInfinity => Some(f64::INFINITY),
            Self::NegativeInfinity => Some(f64::NEG_INFINITY),
        }
    }
    fn json(&self) -> String {
        match self {
            Self::Ordinary(n) => {
                super::super::version::python_text(&serde_json::Value::Number(n.clone()), false)
            }
            Self::Nan => "NaN".into(),
            Self::PositiveInfinity => "Infinity".into(),
            Self::NegativeInfinity => "-Infinity".into(),
        }
    }
    fn python(&self) -> String {
        match self {
            Self::Nan => "nan".into(),
            Self::PositiveInfinity => "inf".into(),
            Self::NegativeInfinity => "-inf".into(),
            _ => self.json(),
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(Number),
    String(Text),
    Array(Vec<Value>),
    Object(Vec<(Text, Value)>),
}
static NULL: Value = Value::Null;
impl Value {
    pub fn as_str(&self) -> Option<&str> {
        if let Self::String(s) = self {
            s.valid.as_deref()
        } else {
            None
        }
    }
    pub fn as_array(&self) -> Option<&Vec<Value>> {
        if let Self::Array(v) = self {
            Some(v)
        } else {
            None
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        if let Self::Number(n) = self {
            n.as_f64()
        } else {
            None
        }
    }
    pub fn is_integer(&self) -> bool {
        match self {
            Self::Bool(_) => true,
            Self::Number(Number::Ordinary(n)) => !n.to_string().contains(['.', 'e', 'E']),
            _ => false,
        }
    }
    pub fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }
    pub fn is_object(&self) -> bool {
        matches!(self, Self::Object(_))
    }
    pub fn is_string(&self) -> bool {
        matches!(self, Self::String(_))
    }
    pub fn get(&self, key: &str) -> Option<&Value> {
        if let Self::Object(items) = self {
            items
                .iter()
                .find(|(k, _)| k.valid.as_deref() == Some(key))
                .map(|(_, v)| v)
        } else {
            None
        }
    }
    fn insert(&mut self, key: Text, value: Value) {
        if let Self::Object(items) = self {
            if let Some((_, v)) = items.iter_mut().find(|(k, _)| *k == key) {
                *v = value;
            } else {
                items.push((key, value));
            }
        }
    }
    pub fn object(items: Vec<(&str, Value)>) -> Self {
        let mut value = Self::Object(vec![]);
        for (key, v) in items {
            value.insert(key.into(), v);
        }
        value
    }
    pub fn python(&self, nested: bool) -> String {
        match self {
            Self::Null => "None".into(),
            Self::Bool(b) => if *b { "True" } else { "False" }.into(),
            Self::Number(n) => n.python(),
            Self::String(s) => {
                if nested {
                    s.repr()
                } else {
                    s.clean()
                }
            }
            Self::Array(v) => format!(
                "[{}]",
                v.iter()
                    .map(|v| v.python(true))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            Self::Object(v) => format!(
                "{{{}}}",
                v.iter()
                    .map(|(k, v)| format!("{}: {}", k.repr(), v.python(true)))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
    pub fn pretty(&self, depth: usize) -> String {
        let indent = "  ".repeat(depth + 1);
        let close = "  ".repeat(depth);
        match self {
            Self::Array(v) if !v.is_empty() => format!(
                "[\n{indent}{}\n{close}]",
                v.iter()
                    .map(|v| v.pretty(depth + 1))
                    .collect::<Vec<_>>()
                    .join(&format!(",\n{indent}"))
            ),
            Self::Object(v) if !v.is_empty() => format!(
                "{{\n{indent}{}\n{close}}}",
                v.iter()
                    .map(|(k, v)| format!("{}: {}", k.quoted(), v.pretty(depth + 1)))
                    .collect::<Vec<_>>()
                    .join(&format!(",\n{indent}"))
            ),
            _ => self.to_string(),
        }
    }
}
impl Index<&str> for Value {
    type Output = Value;
    fn index(&self, k: &str) -> &Value {
        self.get(k).unwrap_or(&NULL)
    }
}
impl IndexMut<&str> for Value {
    fn index_mut(&mut self, k: &str) -> &mut Value {
        if self.is_null() {
            *self = Self::Object(vec![]);
        }
        if self.get(k).is_none() {
            self.insert(k.into(), Self::Null);
        }
        if let Self::Object(v) = self {
            &mut v
                .iter_mut()
                .find(|(key, _)| key.valid.as_deref() == Some(k))
                .unwrap()
                .1
        } else {
            panic!("object index")
        }
    }
}
impl PartialEq<&str> for Value {
    fn eq(&self, v: &&str) -> bool {
        self.as_str() == Some(*v)
    }
}
impl PartialEq<String> for Value {
    fn eq(&self, v: &String) -> bool {
        self.as_str() == Some(v.as_str())
    }
}
impl PartialEq<bool> for Value {
    fn eq(&self, v: &bool) -> bool {
        matches!(self,Self::Bool(b) if b==v)
    }
}
impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            Self::Null => "null".into(),
            Self::Bool(v) => v.to_string(),
            Self::Number(n) => n.json(),
            Self::String(s) => s.quoted(),
            Self::Array(v) => format!(
                "[{}]",
                v.iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
                    .join(",")
            ),
            Self::Object(v) => format!(
                "{{{}}}",
                v.iter()
                    .map(|(k, v)| format!("{}:{v}", k.quoted()))
                    .collect::<Vec<_>>()
                    .join(",")
            ),
        };
        f.write_str(&text)
    }
}
impl From<String> for Value {
    fn from(v: String) -> Self {
        Self::String(v.as_str().into())
    }
}
impl From<&str> for Value {
    fn from(v: &str) -> Self {
        Self::String(v.into())
    }
}
impl From<&String> for Value {
    fn from(v: &String) -> Self {
        Self::from(v.as_str())
    }
}
impl From<std::borrow::Cow<'_, str>> for Value {
    fn from(v: std::borrow::Cow<'_, str>) -> Self {
        Self::from(v.as_ref())
    }
}
impl From<&Value> for Value {
    fn from(v: &Value) -> Self {
        v.clone()
    }
}
impl From<bool> for Value {
    fn from(v: bool) -> Self {
        Self::Bool(v)
    }
}
impl<T: Into<Value>> From<Option<T>> for Value {
    fn from(v: Option<T>) -> Self {
        v.map_or(Self::Null, Into::into)
    }
}
impl<T: Clone + Into<Value>> From<&Option<T>> for Value {
    fn from(v: &Option<T>) -> Self {
        Self::from(v.clone())
    }
}
impl From<Vec<Value>> for Value {
    fn from(v: Vec<Value>) -> Self {
        Self::Array(v)
    }
}
macro_rules! integers { ($($t:ty),*) => { $(impl From<$t> for Value { fn from(v:$t)->Self { Self::Number(Number::Ordinary(v.into())) } })* }; }
integers!(i32, i64, u32, u64);
macro_rules! json {
    ({$($key:literal:$value:expr),* $(,)?}) => { $crate::cli::lease::json::Value::object(vec![$(($key,$crate::cli::lease::json::Value::from($value))),*]) };
    ([]) => { $crate::cli::lease::json::Value::Array(vec![]) };
    ($value:expr) => { $crate::cli::lease::json::Value::from($value) };
}
pub(super) use json;

pub fn from_str(text: &str) -> Result<Value, ()> {
    parse(&text.chars().map(u32::from).collect::<Vec<_>>())
}
pub fn from_slice(bytes: &[u8]) -> Result<Value, ()> {
    // json.loads(bytes), used by the Python HTTP peer, detects UTF-8/16/32
    // and decodes with surrogatepass. A file read as text has no BOM removal.
    let (width, little, skip) = if bytes.starts_with(&[0, 0, 254, 255]) {
        (4, false, 4)
    } else if bytes.starts_with(&[255, 254, 0, 0]) {
        (4, true, 4)
    } else if bytes.starts_with(&[254, 255]) {
        (2, false, 2)
    } else if bytes.starts_with(&[255, 254]) {
        (2, true, 2)
    } else if bytes.starts_with(&[239, 187, 191]) {
        (1, false, 3)
    } else if bytes.len() >= 4 && bytes[0] == 0 && bytes[1] == 0 {
        (4, false, 0)
    } else if bytes.len() >= 4 && bytes[1] == 0 && bytes[2] == 0 && bytes[3] == 0 {
        (4, true, 0)
    } else if bytes.len() >= 2 && bytes[0] == 0 {
        (2, false, 0)
    } else if bytes.len() >= 2 && bytes[1] == 0 {
        (2, true, 0)
    } else {
        (1, false, 0)
    };
    let bytes = &bytes[skip..];
    let mut codepoints = vec![];
    if width == 1 {
        let mut pos = 0;
        while pos < bytes.len() {
            let first = bytes[pos];
            let (length, mut code) = match first {
                0..=127 => (1, u32::from(first)),
                194..=223 => (2, u32::from(first & 31)),
                224..=239 => (3, u32::from(first & 15)),
                240..=244 => (4, u32::from(first & 7)),
                _ => return Err(()),
            };
            for offset in 1..length {
                let b = *bytes.get(pos + offset).ok_or(())?;
                if b & 192 != 128 {
                    return Err(());
                }
                code = code * 64 + u32::from(b & 63);
            }
            if (length == 2 && code < 128)
                || (length == 3 && code < 2048)
                || (length == 4 && !(65536..=0x10ffff).contains(&code))
            {
                return Err(());
            }
            codepoints.push(code);
            pos += length;
        }
    } else {
        if !bytes.len().is_multiple_of(width) {
            return Err(());
        }
        for chunk in bytes.chunks_exact(width) {
            let code = if little {
                chunk
                    .iter()
                    .rev()
                    .fold(0u32, |n, b| n * 256 + u32::from(*b))
            } else {
                chunk.iter().fold(0u32, |n, b| n * 256 + u32::from(*b))
            };
            if code > 0x10ffff {
                return Err(());
            }
            codepoints.push(code);
        }
    }
    if width == 2 {
        // UTF-16 decoding combines valid pairs. UTF-8/32 surrogatepass keeps
        // each raw surrogate as a separate Python character.
        let mut decoded = Vec::with_capacity(codepoints.len());
        let mut points = codepoints.into_iter().peekable();
        while let Some(high) = points.next() {
            if (0xd800..=0xdbff).contains(&high)
                && points
                    .peek()
                    .is_some_and(|low| (0xdc00..=0xdfff).contains(low))
            {
                let low = points.next().unwrap();
                decoded.push(0x10000 + ((high - 0xd800) << 10) + low - 0xdc00);
            } else {
                decoded.push(high);
            }
        }
        return parse(&decoded);
    }
    parse(&codepoints)
}
fn parse(text: &[u32]) -> Result<Value, ()> {
    let mut parser = Parser {
        text,
        pos: 0,
        depth: 0,
    };
    let value = parser.value()?;
    parser.space();
    if parser.pos == text.len() {
        Ok(value)
    } else {
        Err(())
    }
}
struct Parser<'a> {
    text: &'a [u32],
    pos: usize,
    depth: usize,
}
impl Parser<'_> {
    fn space(&mut self) {
        while self
            .text
            .get(self.pos)
            .is_some_and(|b| [32, 9, 13, 10].contains(b))
        {
            self.pos += 1;
        }
    }
    fn take(&mut self, token: &str) -> bool {
        if self.text[self.pos..]
            .iter()
            .copied()
            .take(token.len())
            .eq(token.bytes().map(u32::from))
        {
            self.pos += token.len();
            true
        } else {
            false
        }
    }
    fn string(&mut self) -> Result<Text, ()> {
        if !self.take("\"") {
            return Err(());
        }
        let mut points = vec![];
        loop {
            let c = *self.text.get(self.pos).ok_or(())?;
            self.pos += 1;
            match c {
                34 => return Ok(Text::new(points)),
                92 => {
                    let e = self.text.get(self.pos).copied().ok_or(())?;
                    self.pos += 1;
                    points.push(match e {
                        34 => 34,
                        92 => 92,
                        47 => 47,
                        98 => 8,
                        102 => 12,
                        110 => 10,
                        114 => 13,
                        116 => 9,
                        117 => {
                            let high = self.hex(self.pos)?;
                            self.pos += 4;
                            // Only adjacent JSON escapes combine, never a raw
                            // surrogate next to an escaped or raw surrogate.
                            if (0xd800..=0xdbff).contains(&high)
                                && self.text.get(self.pos..self.pos + 2) == Some(&[92, 117])
                                && let Ok(low) = self.hex(self.pos + 2)
                                && (0xdc00..=0xdfff).contains(&low)
                            {
                                self.pos += 6;
                                0x10000 + ((high - 0xd800) << 10) + low - 0xdc00
                            } else {
                                high
                            }
                        }
                        _ => return Err(()),
                    });
                }
                c if c < 32 => return Err(()),
                c => points.push(c),
            }
        }
    }
    fn hex(&self, pos: usize) -> Result<u32, ()> {
        self.text
            .get(pos..pos + 4)
            .ok_or(())?
            .iter()
            .try_fold(0, |n, c| {
                let digit = match *c {
                    48..=57 => c - 48,
                    65..=70 => c - 65 + 10,
                    97..=102 => c - 97 + 10,
                    _ => return Err(()),
                };
                Ok(n * 16 + digit)
            })
    }
    fn value(&mut self) -> Result<Value, ()> {
        self.space();
        self.depth += 1;
        if self.depth > 1000 {
            return Err(());
        }
        let value = if self.take("null") {
            Value::Null
        } else if self.take("true") {
            Value::Bool(true)
        } else if self.take("false") {
            Value::Bool(false)
        } else if self.take("NaN") {
            Value::Number(Number::Nan)
        } else if self.take("Infinity") {
            Value::Number(Number::PositiveInfinity)
        } else if self.take("-Infinity") {
            Value::Number(Number::NegativeInfinity)
        } else if self.text.get(self.pos) == Some(&34) {
            Value::String(self.string()?)
        } else if self.take("[") {
            let mut values = vec![];
            self.space();
            if !self.take("]") {
                loop {
                    values.push(self.value()?);
                    self.space();
                    if self.take("]") {
                        break;
                    }
                    if !self.take(",") {
                        return Err(());
                    }
                }
            }
            Value::Array(values)
        } else if self.take("{") {
            let mut value = Value::Object(vec![]);
            self.space();
            if !self.take("}") {
                loop {
                    self.space();
                    let key = self.string()?;
                    self.space();
                    if !self.take(":") {
                        return Err(());
                    }
                    value.insert(key, self.value()?);
                    self.space();
                    if self.take("}") {
                        break;
                    }
                    if !self.take(",") {
                        return Err(());
                    }
                }
            }
            value
        } else {
            let start = self.pos;
            while self
                .text
                .get(self.pos)
                .is_some_and(|b| b"-+0123456789.eE".iter().any(|c| u32::from(*c) == *b))
            {
                self.pos += 1;
            }
            let token: String = self.text[start..self.pos]
                .iter()
                .map(|c| char::from_u32(*c).unwrap())
                .collect();
            let n = serde_json::from_str::<serde_json::Number>(&token).map_err(|_| ())?;
            // Python's float decoder permits overflow and underflow, while its
            // integer decoder enforces the interpreter's decimal digit limit.
            if token.contains(['.', 'e', 'E']) {
                match token.parse::<f64>().map_err(|_| ())? {
                    n if n == f64::INFINITY => Value::Number(Number::PositiveInfinity),
                    n if n == f64::NEG_INFINITY => Value::Number(Number::NegativeInfinity),
                    _ => Value::Number(Number::Ordinary(n)),
                }
            } else {
                if token.trim_start_matches('-').len() > 4300 {
                    return Err(());
                }
                Value::Number(Number::Ordinary(if token == "-0" { 0.into() } else { n }))
            }
        };
        self.depth -= 1;
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preserves_python_json_values_and_duplicate_key_order() {
        let value=from_str(r#"{"label":"\ud800","end":NaN,"pos":Infinity,"neg":-Infinity,"label":"\udfff","overflow":1e400}"#).unwrap();
        assert_eq!(
            value.to_string(),
            r#"{"label":"\udfff","end":NaN,"pos":Infinity,"neg":-Infinity,"overflow":Infinity}"#
        );
        assert_eq!(value["label"].python(false), "?");
        assert!(value["label"].is_string());
        assert_eq!(value["end"].python(false), "nan");
    }
    #[test]
    fn invalid_json_and_integer_digit_limit_are_rejected() {
        for raw in [r#"{"x":"\uqqqq"}"#, "[1,]", "01", "true false", "NaNx"] {
            assert!(from_str(raw).is_err(), "{raw}");
        }
        assert!(from_str(&"1".repeat(4301)).is_err());
    }
    #[test]
    fn byte_decoding_preserves_surrogatepass_and_encoding_detection() {
        assert_eq!(
            from_slice(b"\"\xed\xa0\x80\"").unwrap().to_string(),
            r#""\ud800""#
        );
        assert_eq!(
            from_slice(b"\xff\xfe\"\0\0\xd8\"\0").unwrap().to_string(),
            r#""\ud800""#
        );
        assert_eq!(from_slice(b"\0\0\0[\0\0\0]").unwrap(), Value::Array(vec![]));
        assert!(from_str("\u{feff}{}").is_err());
        assert!(from_slice(b"\xed\xa0\x80").is_err());
    }
    #[test]
    fn raw_surrogates_remain_distinct_from_escaped_pairs() {
        let raw = from_slice(b"\"\xed\xa0\x80\xed\xb0\x80\"").unwrap();
        let escaped = from_str(r#""\ud800\udc00""#).unwrap();
        assert_ne!(raw, escaped);
        assert_eq!(raw.as_str(), None);
        assert_eq!(raw.python(false), "??");
        assert_eq!(raw.python(true), "'\\ud800\\udc00'");
        assert_eq!(escaped.as_str(), Some("\u{10000}"));
        assert_eq!(raw.to_string(), escaped.to_string());
        for mixed in [
            b"\"\xed\xa0\x80\\udc00\"".as_slice(),
            b"\"\\ud800\xed\xb0\x80\"".as_slice(),
        ] {
            assert_eq!(from_slice(mixed).unwrap(), raw);
        }
        let object = from_slice(b"{\"\xed\xa0\x80\xed\xb0\x80\":1,\"\\ud800\\udc00\":2}").unwrap();
        assert_eq!(object.python(true), "{'\\ud800\\udc00': 1, '\u{10000}': 2}");
        let utf16 = b"\xff\xfe\"\0\0\xd8\0\xdc\"\0";
        assert_eq!(from_slice(utf16).unwrap(), escaped);
        let utf32 = b"\xff\xfe\0\0\"\0\0\0\0\xd8\0\0\0\xdc\0\0\"\0\0\0";
        assert_eq!(from_slice(utf32).unwrap(), raw);
    }
}
