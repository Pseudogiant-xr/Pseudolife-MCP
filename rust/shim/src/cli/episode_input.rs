//! Hook-owned JSON fields; filesystem strings retain escaped surrogate units.
use serde::{Deserialize, Deserializer, de};
use std::fmt;

pub(super) struct Text(pub Vec<u32>);

impl<'de> Deserialize<'de> for Text {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> de::Visitor<'de> for Visitor {
            type Value = Text;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a hook JSON string")
            }
            fn visit_bytes<E: de::Error>(self, mut bytes: &[u8]) -> Result<Text, E> {
                let mut points = Vec::new();
                // serde_json's byte-string decoder emits WTF-8 for lone \u units.
                while !bytes.is_empty() {
                    match std::str::from_utf8(bytes) {
                        Ok(text) => {
                            points.extend(text.chars().map(u32::from));
                            break;
                        }
                        Err(error) => {
                            let valid = error.valid_up_to();
                            points.extend(
                                std::str::from_utf8(&bytes[..valid])
                                    .unwrap()
                                    .chars()
                                    .map(u32::from),
                            );
                            bytes = &bytes[valid..];
                            if bytes.len() < 3
                                || bytes[0] != 0xed
                                || !(0xa0..=0xbf).contains(&bytes[1])
                                || !(0x80..=0xbf).contains(&bytes[2])
                            {
                                return Err(E::custom("invalid hook string encoding"));
                            }
                            points.push(
                                (u32::from(bytes[0] & 15) << 12)
                                    | (u32::from(bytes[1] & 63) << 6)
                                    | u32::from(bytes[2] & 63),
                            );
                            bytes = &bytes[3..];
                        }
                    }
                }
                Ok(Text(points))
            }
        }
        deserializer.deserialize_bytes(Visitor)
    }
}

pub(super) struct Hook {
    pub key: Option<Text>,
    pub cwd: Option<Text>,
}

pub(super) fn parse(text: &str) -> Option<Hook> {
    let (mut quoted, mut escaped) = (false, false);
    for byte in text.bytes() {
        if quoted && byte < 32 {
            return None;
        }
        if escaped {
            escaped = false;
        } else if quoted && byte == b'\\' {
            escaped = true;
        } else if byte == b'"' {
            quoted = !quoted;
        }
    }
    let encoded = super::super::doorbell_seen::json::protect_keys(text);
    serde_json::from_str(&encoded).ok()
}

impl<'de> Deserialize<'de> for Hook {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> de::Visitor<'de> for Visitor {
            type Value = Hook;
            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a hook JSON object")
            }
            fn visit_map<M: de::MapAccess<'de>>(self, mut map: M) -> Result<Hook, M::Error> {
                let mut hook = Hook {
                    key: None,
                    cwd: None,
                };
                while let Some(field) = map.next_key::<String>()? {
                    match field.as_str() {
                        // parse prefixes every key before serde sees ignored Values.
                        "\0session_id" => hook.key = Some(map.next_value::<Text>()?),
                        "\0cwd" => hook.cwd = map.next_value::<Option<Text>>()?,
                        _ => {
                            map.next_value::<serde_json::Value>()?;
                        }
                    }
                }
                Ok(hook)
            }
        }
        deserializer.deserialize_map(Visitor)
    }
}

pub(super) fn quoted(points: &[u32]) -> String {
    use fmt::Write;
    let mut result = String::from("\"");
    for point in points {
        match *point {
            34 => result.push_str("\\\""),
            92 => result.push_str("\\\\"),
            8 => result.push_str("\\b"),
            9 => result.push_str("\\t"),
            10 => result.push_str("\\n"),
            12 => result.push_str("\\f"),
            13 => result.push_str("\\r"),
            32..=126 => result.push(char::from_u32(*point).unwrap()),
            0..=65535 => {
                let _ = write!(result, "\\u{point:04x}");
            }
            _ => {
                for unit in char::from_u32(*point).unwrap().encode_utf16(&mut [0; 2]) {
                    let _ = write!(result, "\\u{unit:04x}");
                }
            }
        }
    }
    result.push('"');
    result
}

#[cfg(test)]
mod tests {
    #[test]
    fn private_number_keys_in_ignored_episode_fields_are_admitted() {
        // episode_cli.py:36,63: json.loads admits these ignored nested objects.
        for token in ["not a number", "123"] {
            let text = format!(
                r#"{{"session_id":"sess-1","cwd":"/project","other":[{{"$serde_json::private::Number":"{token}"}}]}}"#
            );
            let hook = super::parse(&text).unwrap();
            assert_eq!(super::quoted(&hook.key.unwrap().0), "\"sess-1\"");
            assert_eq!(super::quoted(&hook.cwd.unwrap().0), "\"/project\"");
        }
    }

    #[test]
    fn admitted_strings_keep_exact_ascii_json_and_filesystem_units() {
        let hook =
            super::parse(r#"{"session_id":"mémoire 🧠\t\"\\","cwd":"/missing-\udc80"}"#).unwrap();
        assert_eq!(
            super::quoted(&hook.key.unwrap().0),
            r#""m\u00e9moire \ud83e\udde0\t\"\\""#
        );
        assert_eq!(super::quoted(&hook.cwd.unwrap().0), r#""/missing-\udc80""#);
        let hook = super::parse(r#"{"session_id":"old","session_id":"new","cwd":null}"#).unwrap();
        assert_eq!(super::quoted(&hook.key.unwrap().0), "\"new\"");
        assert!(hook.cwd.is_none());
    }
}
