//! Shared Python JSON domain; the data module retains the reviewed lease parser.
#[path = "python_json_data.rs"]
#[allow(dead_code, unused_imports, unused_macros)]
pub(super) mod json;

fn repr(value: &str) -> String {
    super::mode_repr(value)
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
