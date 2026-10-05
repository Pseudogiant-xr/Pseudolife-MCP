//! Shared Python JSON domain; the data module retains the reviewed lease parser.
#[path = "python_json_data.rs"]
#[allow(dead_code, unused_imports, unused_macros)]
pub(super) mod json;

fn repr(value: &str) -> String {
    super::mode_repr(value)
}

pub(super) fn digit_limit() -> Option<usize> {
    match std::env::var("PYTHONINTMAXSTRDIGITS")
        .ok()
        .and_then(|v| v.trim().parse::<i32>().ok())
    {
        Some(0) => None,
        Some(limit) if limit >= 640 => Some(limit as usize),
        _ => Some(4300),
    }
}

pub(super) fn hook_input(text: &str) -> Result<json::Value, ()> {
    // Public -m briefing (health/content) and prompt-hook on Python 3.11,
    // Windows/Linux, 2026-10-06: 989 nested containers accepted, 990 refused.
    // The reused parser's own limit is 1000; retain the caller boundary here.
    let mut depth = 0usize;
    let mut quoted = false;
    let mut escaped = false;
    for byte in text.bytes() {
        if quoted {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
        } else {
            match byte {
                b'"' => quoted = true,
                b'[' | b'{' => {
                    depth += 1;
                    if depth > 989 {
                        return Err(());
                    }
                }
                b']' | b'}' => depth = depth.saturating_sub(1),
                _ => {}
            }
        }
    }
    json::from_str_with_limit(text, digit_limit())
}

// Python strings may contain codepoints that are not Unicode scalar values.
pub(super) fn points(value: &json::Value) -> Option<&[u32]> {
    match value {
        json::Value::String(text) => Some(text.codepoints()),
        _ => None,
    }
}
pub(super) fn string(points: &[u32]) -> json::Value {
    let mut escaped = String::from("\"");
    for point in points {
        use std::fmt::Write;
        if *point <= 0xffff {
            let _ = write!(escaped, "\\u{point:04x}");
        } else {
            let mut units = [0; 2];
            for unit in char::from_u32(*point).unwrap().encode_utf16(&mut units) {
                let _ = write!(escaped, "\\u{unit:04x}");
            }
        }
    }
    escaped.push('"');
    json::from_str(&escaped).unwrap()
}
