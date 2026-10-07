//! The audited configuration fields of the native sent boundary.
use yaml_rust2::{
    Yaml, YamlLoader,
    parser::{Event, EventReceiver, Parser},
    scanner::TScalarStyle,
};

#[derive(Debug, PartialEq)]
pub struct SentConfig {
    pub enabled: bool,
    pub allowed: Vec<String>,
    pub rp_id: String,
    pub origin: String,
}
impl Default for SentConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            allowed: vec!["default".into()],
            rp_id: String::new(),
            origin: String::new(),
        }
    }
}
fn refusal(rule: &str) -> String {
    format!("config-yaml-typed: {rule}")
}

// Inspect presentation events before the loader loses quoting/tag information.
// The YAML library owns scanning, nesting, aliases and duplicate detection.
#[derive(Default)]
struct Presentation {
    problem: Option<&'static str>,
}
impl EventReceiver for Presentation {
    fn on_event(&mut self, event: Event) {
        if self.problem.is_some() {
            return;
        }
        match event {
            Event::Scalar(value, style, _, tag) => {
                if tag.is_some() {
                    self.problem = Some("explicit tags unsupported");
                } else if style == TScalarStyle::Plain && ambiguous(&value) {
                    self.problem = Some("YAML 1.1/1.2 scalar disagreement; quote the string");
                } else if style == TScalarStyle::Plain && integer_outside_loader(&value) {
                    // The loader falls back to String beyond i64. Never let
                    // a numeric scalar satisfy an audited string field.
                    self.problem = Some("integer scalar outside YAML loader domain");
                }
            }
            Event::MappingStart(_, Some(_)) | Event::SequenceStart(_, Some(_)) => {
                self.problem = Some("explicit tags unsupported")
            }
            _ => (),
        }
    }
}
fn integer_outside_loader(value: &str) -> bool {
    let number = value.trim_start_matches(['+', '-']);
    if let Some(hex) = number.strip_prefix("0x") {
        return !hex.is_empty()
            && hex.bytes().all(|b| b.is_ascii_hexdigit())
            && matches!(Yaml::from_str(value), Yaml::String(_));
    }
    !number.is_empty()
        && number.bytes().all(|b| b.is_ascii_digit())
        && matches!(Yaml::from_str(value), Yaml::String(_))
}
fn ambiguous(value: &str) -> bool {
    if [
        "yes", "Yes", "YES", "no", "No", "NO", "on", "On", "ON", "off", "Off", "OFF",
    ]
    .contains(&value)
    {
        return true;
    }
    let number = value.trim_start_matches(['+', '-']);
    let numeric_start = number.as_bytes().first().is_some_and(u8::is_ascii_digit)
        || number.starts_with('.') && number.as_bytes().get(1).is_some_and(u8::is_ascii_digit);
    if !numeric_start {
        return false;
    }
    if number.contains('_') || number.starts_with("0o") || number.starts_with("0b") {
        return true;
    }
    if number.starts_with('0') && number.len() > 1 && number.bytes().all(|b| b.is_ascii_digit()) {
        return true;
    }
    // PyYAML resolves base-60 and timestamp scalars; YAML 1.2 core does not.
    if number.contains(':') && number.split(':').all(|part| part.parse::<f64>().is_ok()) {
        return true;
    }
    if number.len() >= 10
        && number.as_bytes()[4] == b'-'
        && number.as_bytes()[7] == b'-'
        && number.as_bytes()[..4].iter().all(u8::is_ascii_digit)
        && number.as_bytes()[5..7].iter().all(u8::is_ascii_digit)
        && number.as_bytes()[8..10].iter().all(u8::is_ascii_digit)
    {
        return true;
    }
    if value.parse::<f64>().is_ok() {
        if let Some((mantissa, exponent)) = value.split_once(['e', 'E']) {
            // PyYAML's 1.1 float resolver needs both a dot and an exponent sign.
            return !mantissa.contains('.') || !exponent.starts_with(['+', '-']);
        }
    }
    false
}
fn map<'a>(value: &'a Yaml, path: &str) -> Result<&'a yaml_rust2::yaml::Hash, String> {
    value
        .as_hash()
        .ok_or_else(|| refusal(&format!("{path} must be a mapping")))
}
fn member<'a>(value: &'a Yaml, key: &str) -> Option<&'a Yaml> {
    value.as_hash()?.get(&Yaml::String(key.into()))
}
fn string(value: &Yaml, path: &str) -> Result<String, String> {
    value
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| refusal(&format!("{path} must be a string")))
}
pub fn parse(text: &str) -> Result<SentConfig, String> {
    let mut presentation = Presentation::default();
    Parser::new_from_str(text)
        .load(&mut presentation, true)
        .map_err(|_| refusal("invalid YAML"))?;
    if let Some(problem) = presentation.problem {
        return Err(refusal(problem));
    }
    let docs = YamlLoader::load_from_str(text).map_err(|error| {
        refusal(if error.info().contains("duplicate") {
            "duplicate mapping key"
        } else {
            "invalid YAML"
        })
    })?;
    let mut config = SentConfig::default();
    if docs.is_empty() {
        return Ok(config);
    }
    if docs.len() != 1 {
        return Err(refusal("exactly one YAML document required"));
    }
    let root = &docs[0];
    if matches!(root, Yaml::Null | Yaml::BadValue) {
        return Ok(config);
    }
    map(root, "root")?;
    if let Some(coordination) = member(root, "coordination") {
        map(coordination, "coordination")?;
        if let Some(enabled) = member(coordination, "enabled") {
            config.enabled = enabled
                .as_bool()
                .ok_or_else(|| refusal("coordination.enabled must be a boolean"))?;
        }
        if let Some(allowed) = member(coordination, "allowed_principals") {
            config.allowed = allowed
                .as_vec()
                .ok_or_else(|| refusal("coordination.allowed_principals must be a sequence"))?
                .iter()
                .map(|v| string(v, "coordination.allowed_principals member"))
                .collect::<Result<_, _>>()?;
        }
        if let Some(maintainer) = member(coordination, "maintainer") {
            map(maintainer, "coordination.maintainer")?;
            if let Some(v) = member(maintainer, "rp_id") {
                config.rp_id = string(v, "coordination.maintainer.rp_id")?;
            }
            if let Some(v) = member(maintainer, "origin") {
                config.origin = string(v, "coordination.maintainer.origin")?;
            }
        }
    }
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn installer_shape_and_quoted_strings() {
        let config = parse("embedding:\n  device: cpu\n  backend: torch\n  cpu_dtype: fp32\nmemory:\n  dream:\n    enabled: false\nupdates:\n  check_releases: false\ncoordination:\n  enabled: true\n  allowed_principals: ['yes', \"on\"]\n  maintainer:\n    rp_id: localhost\n    origin: http://localhost\n").unwrap();
        assert_eq!(config.allowed, ["yes", "on"]);
        assert_eq!(config.rp_id, "localhost");
        assert_eq!(parse("").unwrap(), SentConfig::default());
        assert_eq!(parse("{}").unwrap(), SentConfig::default());
    }
    #[test]
    fn required_refusals() {
        for scalar in [
            "yes",
            "no",
            "on",
            "off",
            "012",
            "0o12",
            "0b10",
            "1:20",
            "1:20.5",
            "1_000",
            "1.2_3",
            "1e3",
            "1.2e3",
            "2026-10-07",
        ] {
            assert!(
                parse(&format!("ignored: {scalar}\n"))
                    .unwrap_err()
                    .starts_with("config-yaml-typed:"),
                "{scalar}"
            );
            assert!(
                parse(&format!("ignored: '{scalar}'\n")).is_ok(),
                "quoted {scalar}"
            );
        }
        for text in [
            "x: !custom value",
            "x: !!str yes",
            "x: !custom []",
            "x: !custom {}",
            "x: 1\nx: 2",
            "coordination: false",
            "coordination:\n  enabled: 'true'",
            "coordination:\n  allowed_principals: default",
            "coordination:\n  allowed_principals: [1]",
            "coordination:\n  maintainer: []",
            "coordination:\n  maintainer:\n    rp_id: 1",
            "coordination:\n  maintainer:\n    origin: null",
            "coordination:\n  maintainer:\n    rp_id: 9999999999999999999999999999999999999",
            "[]",
            "---\n{}\n---\n{}",
        ] {
            assert!(
                parse(text).unwrap_err().starts_with("config-yaml-typed:"),
                "{text}"
            );
        }
    }
}
