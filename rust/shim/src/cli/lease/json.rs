//! Lease input admission accepts producer-shaped UTF-8 JSON and finite numbers.
pub use serde_json::{Value, json};
pub type Text = String;

fn admitted(value: &Value) -> bool {
    match value {
        Value::Number(number) => number.as_f64().is_some_and(f64::is_finite),
        Value::Array(values) => values.iter().all(admitted),
        Value::Object(values) => values.values().all(admitted),
        _ => true,
    }
}
fn checked(value: Value) -> Result<Value, ()> {
    if admitted(&value) { Ok(value) } else { Err(()) }
}
pub fn from_str(text: &str) -> Result<Value, ()> {
    checked(serde_json::from_str(text).map_err(|_| ())?)
}
pub fn from_slice(bytes: &[u8]) -> Result<Value, ()> {
    checked(serde_json::from_slice(bytes).map_err(|_| ())?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn producer_json_preserves_unicode_numbers_and_key_order() {
        let value =
            from_str(r#"{"label":"m\u00e9moire","count":2,"stamp":123.5,"label":"second"}"#)
                .unwrap();
        assert_eq!(value["label"], "second");
        assert_eq!(value["count"], 2);
        assert_eq!(value["stamp"], 123.5);
        assert_eq!(
            value.as_object().unwrap().keys().collect::<Vec<_>>(),
            ["label", "count", "stamp"]
        );
    }
    #[test]
    fn nonproducer_json_is_not_understood() {
        for raw in [
            r#"{"label":"\ud800"}"#,
            "NaN",
            "Infinity",
            "-Infinity",
            "1e400",
            "[1,]",
            "01",
        ] {
            assert!(from_str(raw).is_err(), "{raw}");
        }
        for raw in [
            b"\xff\xfe[\0]\0".as_slice(),
            b"\0\0\0[\0\0\0]",
            b"\"\xed\xa0\x80\"",
        ] {
            assert!(from_slice(raw).is_err());
        }
    }
}
