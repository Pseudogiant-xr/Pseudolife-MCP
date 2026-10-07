//! The audited configuration fields of the native sent boundary.
use regex_lite::Regex;
use std::sync::LazyLock;
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
    // Anchored predicates from PyYAML 6.0.3 resolver.py, not a YAML loader.
    // Compare resolution only; yaml-rust2 still owns all parsing/constructing.
    static BOOL: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$",
        )
        .unwrap()
    });
    static INT: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$").unwrap()
    });
    static FLOAT: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$").unwrap()
    });
    static NULL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^(?:~|null|Null|NULL|)$").unwrap());
    static TIME: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9]{2})?))?)$").unwrap()
    });
    let resolved = Yaml::from_str(value);
    if BOOL.is_match(value) {
        return !matches!(resolved, Yaml::Boolean(_));
    }
    if FLOAT.is_match(value) {
        return !matches!(resolved, Yaml::Real(_));
    }
    if INT.is_match(value) {
        let Yaml::Integer(actual) = resolved else {
            return true;
        };
        let number = value.trim_start_matches(['+', '-']);
        // Both loaders resolve leading-zero digits, but Python uses octal.
        return number.starts_with('0')
            && number.len() > 1
            && number.bytes().all(|b| b.is_ascii_digit())
            && i64::from_str_radix(number, 8)
                .ok()
                .map(|v| if value.starts_with('-') { -v } else { v })
                != Some(actual);
    }
    if NULL.is_match(value) {
        return !matches!(resolved, Yaml::Null);
    }
    TIME.is_match(value) || !matches!(resolved, Yaml::String(_))
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
            let names = allowed
                .as_vec()
                .ok_or_else(|| refusal("coordination.allowed_principals must be a sequence"))?
                .iter()
                .map(|v| string(v, "coordination.allowed_principals member"))
                .collect::<Result<Vec<_>, _>>()?;
            config.allowed.clear();
            for name in names {
                let name = name
                    .trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                    .to_lowercase();
                if name.is_empty() {
                    return Err(refusal(
                        "coordination.allowed_principals member must be nonblank",
                    ));
                }
                if !config.allowed.contains(&name) {
                    config.allowed.push(name);
                }
            }
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
    fn principal_normalization_and_resolver_agreement() {
        assert_eq!(parse("coordination:\n  allowed_principals: [' DEFAULT ', ' DeFaUlT ', ' 1_agent ', '123_name', \"\\u001cDEFAULT\\u001f\"]\n").unwrap().allowed, ["default", "1_agent", "123_name"]);
        assert!(parse("coordination:\n  allowed_principals: [' ']").is_err());
        for scalar in [
            "1_agent",
            "123_name",
            "0o_name",
            "1:99",
            "2026-01-02_note",
            "0",
            "00",
            "true",
            "FALSE",
            ".inf",
            "1.0e+3",
        ] {
            assert!(parse(&format!("ignored: {scalar}\n")).is_ok(), "{scalar}");
        }
        for scalar in ["Null", "NULL", "+.5", "-.5", "08", "-012"] {
            assert!(parse(&format!("ignored: {scalar}\n")).is_err(), "{scalar}");
            assert!(
                parse(&format!("ignored: '{scalar}'\n")).is_ok(),
                "quoted {scalar}"
            );
        }
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
            "2026-1-2T3:04:05",
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
