//! Lazy archive rows as `_read_export` admits them; other domains defer.
use super::super::doorbell_seen::json as scalar_json;
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde_json::Value;
use std::{cell::Cell, collections::HashSet, fmt};

#[derive(Debug)]
pub(super) enum Error {
    Duplicate,
    InvalidRow,
    Deferred,
}

// Check decoded keys before Value construction can discard duplicate evidence.
struct Unique<'a>(&'a Cell<bool>);

impl<'de> DeserializeSeed<'de> for Unique<'_> {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Unique<'_> {
    type Value = ();
    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("JSON with unique decoded object keys")
    }
    fn visit_bool<E: de::Error>(self, _: bool) -> Result<(), E> {
        Ok(())
    }
    fn visit_i64<E: de::Error>(self, _: i64) -> Result<(), E> {
        Ok(())
    }
    fn visit_u64<E: de::Error>(self, _: u64) -> Result<(), E> {
        Ok(())
    }
    fn visit_f64<E: de::Error>(self, _: f64) -> Result<(), E> {
        Ok(())
    }
    fn visit_str<E: de::Error>(self, _: &str) -> Result<(), E> {
        Ok(())
    }
    fn visit_unit<E: de::Error>(self) -> Result<(), E> {
        Ok(())
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<(), A::Error> {
        while sequence.next_element_seed(Unique(self.0))?.is_some() {}
        Ok(())
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        let mut keys = HashSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !keys.insert(key) {
                self.0.set(true);
                return Err(de::Error::custom("duplicate decoded key"));
            }
            map.next_value_seed(Unique(self.0))?;
        }
        Ok(())
    }
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
    let duplicate = Cell::new(false);
    let mut deserializer = serde_json::Deserializer::from_str(text);
    if Unique(&duplicate).deserialize(&mut deserializer).is_err() || deserializer.end().is_err() {
        return Err(if duplicate.get() {
            Error::Duplicate
        } else {
            Error::Deferred
        });
    }
    // Reuse the scalar reader's protection against serde's private Number key;
    // its nonfinite, surrogate and nesting refusals remain deferred domains.
    let value: Value = scalar_json::from_str(text).map_err(|_| Error::Deferred)?;
    // Python's reader refuses (as not JSON) an integer over its digit limit.
    if !super::codec::python_int_domain(&value) {
        return Err(Error::Deferred);
    }
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
}
