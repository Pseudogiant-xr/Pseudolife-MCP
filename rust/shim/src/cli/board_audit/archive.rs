//! Lazy archive rows as `_read_export` admits them; other domains defer.
use super::{super::pyjson, codec};
use serde_json::Value;

#[derive(Debug)]
pub(super) enum Error {
    Duplicate,
    NotJson,
    InvalidRow,
    Deferred,
    Encoding,
}

const COLUMNS: &[&str] = &[
    "seq",
    "event",
    "actor",
    "principal",
    "agent_id",
    "recipient_agent_id",
    "project",
    "task",
    "message_id",
    "created_at",
    "hlc",
    "payload",
    "prev_hash",
    "hash",
];

pub(super) fn row(text: &str) -> Result<(Value, i64, f64), Error> {
    let parsed = pyjson::loads_unique(text, codec::ARCHIVE_DEPTH).map_err(|error| match error {
        None => Error::Duplicate,
        Some(pyjson::Refused::NotJson) => Error::NotJson,
        Some(pyjson::Refused::TooDeep) => Error::Encoding,
    })?;
    let pyjson::J::Dict(items) = parsed else {
        return Err(Error::InvalidRow);
    };
    let mut map = serde_json::Map::new();
    let mut payload = None;
    for (key, item) in items {
        let Some(key) = key.as_str() else {
            continue;
        };
        if key == "payload" {
            payload = Some(item);
            map.insert(key.to_owned(), Value::Null);
        } else if COLUMNS.contains(&key) || matches!(key, "body" | "body_salt") {
            let mut text = String::new();
            codec::python_compact(&item, true, false, &mut text).ok_or(Error::Deferred)?;
            let value = super::super::doorbell_seen::json::value_from_str(&text)
                .map_err(|_| Error::Deferred)?;
            map.insert(key.to_owned(), value);
        }
    }
    let mut value = Value::Object(map);
    let Some(object) = value.as_object() else {
        return Err(Error::InvalidRow);
    };
    if COLUMNS.iter().any(|key| !object.contains_key(*key))
        || [
            "event",
            "actor",
            "principal",
            "agent_id",
            "project",
            "task",
            "hlc",
            "prev_hash",
            "hash",
        ]
        .iter()
        .any(|key| !value[*key].is_string())
        || ["body", "body_salt"].iter().any(|key| {
            object
                .get(*key)
                .is_some_and(|v| !v.is_null() && !v.is_string())
        })
    {
        return Err(Error::InvalidRow);
    }
    let Some(seq) = value["seq"].as_number() else {
        return Err(Error::InvalidRow);
    };
    let token = seq.to_string();
    if token.contains(['.', 'e', 'E']) {
        return Err(Error::InvalidRow);
    }
    let seq = token.parse::<i64>().map_err(|_| Error::Deferred)?;
    let Some(timestamp) = value["created_at"].as_number() else {
        return Err(Error::InvalidRow);
    };
    let created_at = timestamp
        .to_string()
        .parse::<f64>()
        .map_err(|_| Error::Deferred)?;
    if !created_at.is_finite() {
        return Err(Error::Deferred);
    }
    let payload = payload.expect("required payload column was checked");
    let text = if let pyjson::J::Str(text) = payload {
        text
    } else {
        let mut text = String::new();
        codec::python_compact(&payload, false, true, &mut text).ok_or(Error::Encoding)?;
        text
    };
    value["payload"] = Value::String(text);
    Ok((value, seq, created_at))
}

pub(super) fn blank(text: &str) -> bool {
    // Python str.isspace additionally includes the four information separators.
    text.chars()
        .all(|c| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decoded_duplicate_keys_precede_value_construction() {
        for text in [r#"{"a":1,"\u0061":2}"#, r#"{"payload":{"a":1,"a":2}}"#] {
            assert!(matches!(row(text), Err(Error::Duplicate)));
        }
        assert!(matches!(row("{}"), Err(Error::InvalidRow)));
        assert!(blank("\u{1c}\u{85}\u{2000}\r\n"));
    }

    #[test]
    fn reserved_number_key_duplicates_are_object_keys() {
        for text in [
            r#"{"payload":{"$serde_json::private::Number":"1","$serde_json::private::Number":"1"}}"#,
            r#"{"payload":{"$serde_json::private::Number":"1","\u0024serde_json::private::Number":"1"}}"#,
            r#"{"payload":{"\u0024serde_json::private::Number":"1","$serde_json::private::\u004eumber":"1"}}"#,
            r#"{"payload":[{"$serde_json::private::Number":{},"$serde_json::private::Number":{}}]}"#,
        ] {
            assert!(matches!(row(text), Err(Error::Duplicate)), "{text}");
        }
        // A single key is ordinary data, irrespective of its value's type.
        for value in [r#""1""#, "{}", "[]", "1", "true", "null"] {
            let text = format!(r#"{{"payload":{{"$serde_json::private::Number":{value}}}}}"#);
            assert!(matches!(row(&text), Err(Error::InvalidRow)), "{text}");
        }
    }

    #[test]
    fn duplicates_are_reported_when_their_object_closes() {
        let digits = "9".repeat(4301);
        // Python: not JSON (the object never closes / the scan fails first).
        assert!(matches!(row(r#"{"seq":1,"seq":2"#), Err(Error::NotJson)));
        let late = format!(r#"{{"a":1,"a":2,"n":{digits}}}"#);
        assert!(matches!(row(&late), Err(Error::NotJson)));
        // Python: the inner object closes before the long integer is scanned.
        let nested = format!(r#"{{"a":{{"x":1,"x":2}},"n":{digits}}}"#);
        assert!(matches!(row(&nested), Err(Error::Duplicate)));
        let float = format!(r#"{{"a":1,"a":2,"n":{digits}.5}}"#);
        assert!(matches!(row(&float), Err(Error::Duplicate)));
    }
}
