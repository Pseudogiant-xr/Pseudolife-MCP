//! CPython's default JSON whitespace, insertion order and ASCII escaping.
use serde_json::Value;

pub(super) fn dumps(v: &Value, ascii: bool) -> String {
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter()
                .map(|v| dumps(v, ascii))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", quote(k, ascii), dumps(v, ascii)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::String(s) => quote(s, ascii),
        Value::Number(n) if n.to_string().contains(['.', 'e', 'E']) => {
            crate::float_repr::finite_float(n.as_f64().unwrap_or(0.0))
                .unwrap_or_else(|_| n.to_string())
        }
        _ => v.to_string(),
    }
}

fn quote(s: &str, ascii: bool) -> String {
    let encoded = serde_json::to_string(s).expect("string JSON");
    if !ascii {
        return encoded;
    }
    let mut out = String::new();
    for c in encoded.chars() {
        if c <= '\u{7e}' {
            out.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

pub(super) fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}

pub(super) fn repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

pub(super) fn typename(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
        Value::Number(n) => {
            if n.is_f64() {
                "float"
            } else {
                "int"
            }
        }
    }
}

pub(super) fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(value_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", repr(k), value_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => dumps(v, false),
    }
}

fn value_repr(v: &Value) -> String {
    if let Value::String(s) = v {
        repr(s)
    } else {
        text(v)
    }
}

pub(super) fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
