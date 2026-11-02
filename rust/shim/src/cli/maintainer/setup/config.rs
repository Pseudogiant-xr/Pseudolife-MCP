//! File-only maintainer settings. Refuse an unrepresentable rewrite before backup or mutation.
use super::{Error, Result, SETUP};
use crate::pg;
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};
use yaml_rust2::{
    Yaml, YamlLoader,
    parser::{Event, MarkedEventReceiver, Parser, Tag},
    scanner::{Marker, TScalarStyle},
};

pub(super) fn data_dir() -> Result<(PathBuf, bool)> {
    if let Some(value) = std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR").filter(|v| !v.is_empty()) {
        return Ok((PathBuf::from(value), true));
    }
    let default = pg::embedded::default_data_dir(
        pg::embedded::Platform::current(),
        &|key| std::env::var_os(key),
        dirs::home_dir().as_deref(),
    );
    if let Some(default) = default.filter(|p| p.exists()) {
        return Ok((default, true));
    }
    Ok((
        std::env::current_dir()
            .map_err(|_| Error::refused("cannot read the current directory"))?
            .join("data"),
        false,
    ))
}

pub(super) fn path(write: bool) -> Result<PathBuf> {
    if let Some(value) = std::env::var_os("PSEUDOLIFE_MCP_CONFIG").filter(|v| !v.is_empty()) {
        return Ok(native_path(value.into()));
    }
    let (data, known) = data_dir()?;
    if write && !known {
        return Err(Error::refused(format!(
            "cannot tell where this daemon reads its config: set PSEUDOLIFE_MCP_DATA_DIR (or PSEUDOLIFE_MCP_CONFIG) as the daemon has it, then run `{SETUP}` again"
        )));
    }
    Ok(native_path(data.join("config.yaml")))
}

fn native_path(path: PathBuf) -> PathBuf {
    // pathlib renders Windows paths with native separators even when the
    // configuration environment variable was written with forward slashes.
    #[cfg(windows)]
    let path = path.components().collect();
    path
}

pub(super) fn problem(rp: &str, origin: &str) -> Option<String> {
    let text = if rp.is_empty() || origin.is_empty() {
        "unset"
    } else if rp.len() > 253
        || !rp.split('.').all(|part| {
            let bytes = part.as_bytes();
            let alnum = |b: &u8| b.is_ascii_lowercase() || b.is_ascii_digit();
            !bytes.is_empty()
                && bytes.len() <= 63
                && alnum(&bytes[0])
                && alnum(&bytes[bytes.len() - 1])
                && bytes.iter().all(|b| alnum(b) || *b == b'-')
        })
    {
        "rp_id must be a lower-case hostname"
    } else {
        let Some((scheme, rest)) = origin.split_once("://") else {
            return Some("origin must be https".into());
        };
        let scheme = scheme.to_ascii_lowercase();
        let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
        let port = authority.rsplit_once(':').map(|(_, p)| p);
        let port = match port {
            Some("") | None => None,
            Some(p) => match p
                .bytes()
                .all(|b| b.is_ascii_digit())
                .then(|| p.parse::<u16>())
                .transpose()
            {
                Ok(Some(p)) => Some(p),
                _ => return Some("origin has an invalid port".into()),
            },
        };
        if scheme == "http" && rp != "localhost" {
            "plain http is allowed for rp_id localhost only"
        } else if !matches!(scheme.as_str(), "https" | "http") {
            "origin must be https"
        } else if port == Some(if scheme == "https" { 443 } else { 80 }) {
            return Some(format!(
                "origin must leave out the default port :{}",
                port.unwrap()
            ));
        } else {
            let expected = format!(
                "{scheme}://{rp}{}",
                port.map(|p| format!(":{p}")).unwrap_or_default()
            );
            if origin == expected {
                return None;
            }
            "origin must be exactly <scheme>://<rp_id>[:<port>]"
        }
    };
    Some(text.into())
}

pub(super) fn rate_limit_problem(section: &Value) -> Option<String> {
    let cap = section.get("maintainer_per_recipient_per_hour")?;
    if cap
        .as_number()
        .is_some_and(|n| n.to_string().bytes().all(|b| b.is_ascii_digit()))
    {
        None
    } else {
        Some("coordination.maintainer.maintainer_per_recipient_per_hour must be a whole number, 0 or more".into())
    }
}

/// YAML 1.1 scalar resolution through yaml-rust2's event loader. Quoted values
/// stay strings. JSON output is YAML and preserves all represented value types.
#[derive(Default)]
struct Loader {
    loader: YamlLoader,
    problem: Option<String>,
    collection_anchors: std::collections::BTreeSet<usize>,
    shared: bool,
    reading: bool,
    merge_key: Option<String>,
    opaque_prefix: String,
}
impl MarkedEventReceiver for Loader {
    fn on_event(&mut self, event: Event, mark: Marker) {
        if self.problem.is_some() {
            return;
        }
        let event = match event {
            Event::MappingStart(id, ref tag) | Event::SequenceStart(id, ref tag) => {
                if id != 0 {
                    self.collection_anchors.insert(id);
                }
                let safe_tag = tag.as_ref().is_some_and(|t| {
                    t.handle == "tag:yaml.org,2002:"
                        && matches!(t.suffix.as_str(), "map" | "seq" | "set" | "omap" | "pairs")
                });
                if tag.is_some() && !(self.reading && safe_tag) {
                    self.problem = Some("unsupported YAML collection tag".into());
                }
                event
            }
            Event::Alias(id) => {
                if self.collection_anchors.contains(&id) {
                    self.shared = true;
                }
                event
            }
            Event::Scalar(value, style, id, tag)
                if self.reading
                    && (tag.as_ref().is_some_and(|t| {
                        t.handle == "tag:yaml.org,2002:" && t.suffix == "merge"
                    }) || tag.is_none() && style == TScalarStyle::Plain && value == "<<") =>
            {
                Event::Scalar(
                    self.merge_key.clone().unwrap(),
                    TScalarStyle::DoubleQuoted,
                    id,
                    None,
                )
            }
            Event::Scalar(value, style, id, tag) => {
                match scalar(value, style, tag, self.reading, &self.opaque_prefix) {
                    Ok((value, style, tag)) => Event::Scalar(value, style, id, tag),
                    Err(problem) => {
                        self.problem = Some(problem);
                        return;
                    }
                }
            }
            _ => event,
        };
        self.loader.on_event(event, mark);
    }
}

fn scalar(
    mut value: String,
    style: TScalarStyle,
    tag: Option<Tag>,
    reading: bool,
    opaque_prefix: &str,
) -> std::result::Result<(String, TScalarStyle, Option<Tag>), String> {
    if tag.is_none() && style == TScalarStyle::Plain && value == "<<" {
        return Err("config.yaml uses YAML merge keys; edit it on the daemon host instead".into());
    }
    if tag.is_none() && style != TScalarStyle::Plain {
        return Ok((value, style, None));
    }
    let explicit = tag.as_ref().map(|t| format!("{}{}", t.handle, t.suffix));
    let tag = explicit.as_deref();
    if reading
        && matches!(
            tag,
            Some("tag:yaml.org,2002:timestamp" | "tag:yaml.org,2002:binary")
        )
    {
        // Keep distinct scalar identities until projection: converting keys to
        // null here would make otherwise valid mappings fail duplicate checks.
        return Ok((
            format!("{opaque_prefix}{}:{value}", tag.unwrap()),
            TScalarStyle::DoubleQuoted,
            None,
        ));
    }
    if tag.is_some_and(|t| {
        !matches!(
            t,
            "tag:yaml.org,2002:str"
                | "tag:yaml.org,2002:int"
                | "tag:yaml.org,2002:float"
                | "tag:yaml.org,2002:bool"
                | "tag:yaml.org,2002:null"
        )
    }) {
        return Err("config.yaml holds a YAML tag this command cannot rewrite; edit it on the daemon host instead".into());
    }
    if tag == Some("tag:yaml.org,2002:str") {
        return Ok((value, TScalarStyle::DoubleQuoted, None));
    }
    let boolean = regex::Regex::new(
        r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$",
    )
    .unwrap();
    let int = regex::Regex::new(r"^[-+]?(?:0b[0-1_]+|0[0-7_]+|(?:0|[1-9][0-9_]*)|0x[0-9a-fA-F_]+|[1-9][0-9_]*(?::[0-5]?[0-9])+)$").unwrap();
    let float = regex::Regex::new(r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$").unwrap();
    let null = matches!(value.as_str(), "" | "~" | "null" | "Null" | "NULL");
    if tag == Some("tag:yaml.org,2002:bool") || tag.is_none() && boolean.is_match(&value) {
        let lower = value.to_ascii_lowercase();
        if !matches!(
            lower.as_str(),
            "yes" | "on" | "true" | "no" | "off" | "false"
        ) {
            return Err("invalid YAML boolean".into());
        }
        value = matches!(lower.as_str(), "yes" | "on" | "true").to_string();
    } else if tag == Some("tag:yaml.org,2002:null") || tag.is_none() && null {
        value = "null".into();
    } else if tag == Some("tag:yaml.org,2002:int") || tag.is_none() && int.is_match(&value) {
        value = integer(&value).ok_or_else(|| "invalid YAML integer".to_owned())?;
        if !reading && value.parse::<i64>().is_err() {
            return Err("config.yaml holds an integer outside the supported range; edit it on the daemon host instead".into());
        }
    } else if tag == Some("tag:yaml.org,2002:float") || tag.is_none() && float.is_match(&value) {
        let text = value.replace('_', "");
        let (sign, digits) = if let Some(v) = text.strip_prefix('-') {
            (-1.0, v)
        } else {
            (1.0, text.strip_prefix('+').unwrap_or(&text))
        };
        let number = if digits.contains(':') {
            digits
                .split(':')
                .try_fold(0.0, |n, p| p.parse::<f64>().ok().map(|p| n * 60.0 + p))
                .map(|n| n * sign)
        } else {
            text.parse::<f64>().ok()
        };
        let number = match number.filter(|n| n.is_finite()) {
            Some(number) => number,
            None if reading => return Ok(("null".into(), TScalarStyle::Plain, None)),
            None => return Err("config.yaml holds an unsupported non-finite number".into()),
        };
        value = format!("{number:?}");
    } else {
        // Match PyYAML's timestamp resolver, including short month/day fields
        // in datetimes; date-like plain strings must remain writable strings.
        let timestamp = regex::Regex::new(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$").unwrap();
        if tag.is_none() && timestamp.is_match(&value) {
            if reading {
                return Ok((
                    format!("{opaque_prefix}tag:yaml.org,2002:timestamp:{value}"),
                    TScalarStyle::DoubleQuoted,
                    None,
                ));
            }
            return Err("config.yaml holds a YAML timestamp this command cannot rewrite; edit it on the daemon host instead".into());
        }
        return Ok((value, TScalarStyle::DoubleQuoted, None));
    }
    Ok((value, TScalarStyle::Plain, None))
}

fn integer(value: &str) -> Option<String> {
    let digits = value.replace('_', "");
    let (sign, digits) = if let Some(v) = digits.strip_prefix('-') {
        (true, v)
    } else {
        (false, digits.strip_prefix('+').unwrap_or(&digits))
    };
    let (base, parts) = if let Some(v) = digits.strip_prefix("0x") {
        (
            16,
            v.chars()
                .map(|c| c.to_digit(16))
                .collect::<Option<Vec<_>>>()?,
        )
    } else if let Some(v) = digits.strip_prefix("0b") {
        (
            2,
            v.chars()
                .map(|c| c.to_digit(2))
                .collect::<Option<Vec<_>>>()?,
        )
    } else if digits.len() > 1 && digits.starts_with('0') {
        (
            8,
            digits
                .chars()
                .map(|c| c.to_digit(8))
                .collect::<Option<Vec<_>>>()?,
        )
    } else if digits.contains(':') && !digits.starts_with('0') {
        let (first, rest) = digits.split_once(':')?;
        let mut result = integer(first)?;
        for part in rest.split(':') {
            let part = part.parse::<u32>().ok().filter(|n| *n < 60)?;
            decimal_step(&mut result, 60, part);
        }
        return Some(if sign && result != "0" {
            format!("-{result}")
        } else {
            result
        });
    } else {
        (
            10,
            digits
                .chars()
                .map(|c| c.to_digit(10))
                .collect::<Option<Vec<_>>>()?,
        )
    };
    if parts.is_empty() {
        return None;
    }
    let mut result = "0".to_owned();
    for part in parts {
        decimal_step(&mut result, base, part);
    }
    Some(if sign && result != "0" {
        format!("-{result}")
    } else {
        result
    })
}

// PyYAML integers are unbounded. Keep their exact decimal spelling through
// the read-only JSON projection instead of converting them to strings/floats.
fn decimal_step(value: &mut String, base: u32, mut carry: u32) {
    let mut digits = Vec::with_capacity(value.len() + 2);
    for digit in value.bytes().rev() {
        carry += u32::from(digit - b'0') * base;
        digits.push(b'0' + (carry % 10) as u8);
        carry /= 10;
    }
    while carry > 0 {
        digits.push(b'0' + (carry % 10) as u8);
        carry /= 10;
    }
    digits.reverse();
    *value = String::from_utf8(digits).expect("decimal digits");
}

fn json_value(value: &Yaml) -> Result<Value> {
    projected_value(value, None, None)
}

fn projected_value(
    value: &Yaml,
    merge_key: Option<&str>,
    opaque_prefix: Option<&str>,
) -> Result<Value> {
    Ok(match value {
        Yaml::Null | Yaml::BadValue => Value::Null,
        Yaml::Boolean(v) => json!(v),
        Yaml::Integer(v) => json!(v),
        Yaml::String(v) if opaque_prefix.is_some_and(|prefix| v.starts_with(prefix)) => Value::Null,
        Yaml::String(v) => json!(v),
        Yaml::Real(v) => {
            let n = if v
                .strip_prefix('-')
                .unwrap_or(v)
                .bytes()
                .all(|b| b.is_ascii_digit())
            {
                v.parse::<serde_json::Number>().ok()
            } else {
                v.parse::<f64>().ok().and_then(serde_json::Number::from_f64)
            }
            .ok_or_else(|| Error::refused("config.yaml holds an unsupported non-finite number"))?;
            Value::Number(n)
        }
        Yaml::Array(v) => Value::Array(
            v.iter()
                .map(|v| projected_value(v, merge_key, opaque_prefix))
                .collect::<Result<_>>()?,
        ),
        Yaml::Hash(v) => {
            let mut out = serde_json::Map::new();
            if let Some(key) = merge_key
                && let Some(merged) = v.get(&Yaml::String(key.into()))
            {
                let sources = match merged {
                    Yaml::Hash(_) => vec![merged],
                    Yaml::Array(sources) => sources.iter().collect(),
                    _ => return Err(Error::refused("invalid YAML merge value")),
                };
                for source in sources.into_iter().rev() {
                    let Value::Object(source) = projected_value(source, merge_key, opaque_prefix)?
                    else {
                        return Err(Error::refused("invalid YAML merge value"));
                    };
                    out.extend(source);
                }
            }
            for (key, value) in v {
                let Some(key) = key.as_str() else {
                    if merge_key.is_some() {
                        continue;
                    }
                    return Err(Error::refused("config.yaml holds a non-string mapping key"));
                };
                if Some(key) != merge_key
                    && !opaque_prefix.is_some_and(|prefix| key.starts_with(prefix))
                {
                    out.insert(
                        key.into(),
                        projected_value(value, merge_key, opaque_prefix)?,
                    );
                }
            }
            Value::Object(out)
        }
        _ => return Err(Error::refused("config.yaml holds an unsupported value")),
    })
}

pub(super) fn read(path: &Path, rewriting: bool) -> Result<Value> {
    let text = match fs::read_to_string(path) {
        Ok(v) => v,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(json!({})),
        Err(_) => return Err(Error::refused("cannot read the daemon configuration")),
    };
    #[derive(Default)]
    struct Events(Vec<(Event, Marker)>);
    impl MarkedEventReceiver for Events {
        fn on_event(&mut self, event: Event, mark: Marker) {
            self.0.push((event, mark));
        }
    }
    let mut events = Events::default();
    Parser::new_from_str(&text)
        .load(&mut events, true)
        .map_err(|_| Error::refused("cannot read the daemon configuration: invalid YAML"))?;
    // Validate duplicate keys after scalar resolution: the ordinary loader
    // would conflate a plain timestamp key with a quoted string of that date.
    let document_count = events
        .0
        .iter()
        .filter(|(event, _)| matches!(event, Event::DocumentEnd))
        .count();
    // A decoded scalar can contain escaped NULs. Choose a marker absent from
    // every decoded scalar so a quoted literal `<<` never becomes a merge key.
    let mut merge_key = "\0maintainer-merge".to_owned();
    while events
        .0
        .iter()
        .any(|(event, _)| matches!(event, Event::Scalar(value, ..) if value == &merge_key))
    {
        merge_key.push('-');
    }
    // Decoded strings, including escaped NULs, must never impersonate the
    // internal representation of a non-JSON scalar, whether key or value.
    let mut opaque_prefix = "\0maintainer-scalar:".to_owned();
    while events.0.iter().any(
        |(event, _)| matches!(event, Event::Scalar(value, ..) if value.starts_with(&opaque_prefix)),
    ) {
        opaque_prefix.push('-');
    }
    let mut loader = Loader {
        reading: !rewriting,
        merge_key: (!rewriting).then_some(merge_key),
        opaque_prefix,
        ..Default::default()
    };
    for (event, mark) in events.0 {
        loader.on_event(event, mark);
    }
    if let Some(problem) = loader.problem {
        return Err(Error::refused(problem));
    }
    if rewriting && loader.shared {
        return Err(Error::refused(
            "config.yaml shares a mapping or list through a YAML anchor and alias; edit it on the daemon host instead",
        ));
    }
    let docs = loader.loader.documents();
    if docs.len() != document_count {
        return Err(Error::refused(
            "cannot read the daemon configuration: duplicate resolved YAML keys",
        ));
    }
    if docs.len() > 1 {
        return Err(Error::refused(
            "cannot read the daemon configuration: multiple YAML documents",
        ));
    }
    let value = docs
        .first()
        .map(|value| {
            if rewriting {
                json_value(value)
            } else {
                projected_value(
                    value,
                    loader.merge_key.as_deref(),
                    Some(&loader.opaque_prefix),
                )
            }
        })
        .transpose()?
        .unwrap_or_else(|| json!({}));
    Ok(if value.is_object() { value } else { json!({}) })
}

pub(super) fn write(rp: &str, origin: &str) -> Result<Value> {
    if let Some(problem) = problem(rp, origin) {
        return Err(Error::new(problem, 2));
    }
    let path = path(true)?;
    let mut data = read(&path, true)?;
    let coordination = data
        .as_object_mut()
        .unwrap()
        .entry("coordination")
        .or_insert_with(|| json!({}));
    let coordination = coordination
        .as_object_mut()
        .ok_or_else(|| Error::refused("coordination in the config file is not a mapping"))?;
    let section = coordination
        .entry("maintainer")
        .or_insert_with(|| json!({}));
    let section = section.as_object_mut().ok_or_else(|| {
        Error::refused("coordination.maintainer in the config file is not a mapping")
    })?;
    if let Some(problem) = rate_limit_problem(&Value::Object(section.clone())) {
        return Err(Error::new(problem, 2));
    }
    section.insert("rp_id".into(), json!(rp));
    section.insert("origin".into(), json!(origin));
    let bytes = json_yaml(&data).into_bytes();
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent)
        .map_err(|_| Error::refused("cannot create the configuration directory"))?;
    let backup = if path.exists() {
        let backup = parent.join(format!(
            "{}.{}.bak",
            path.file_name().unwrap().to_string_lossy(),
            uuid::Uuid::new_v4().simple()
        ));
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&backup)
            .map_err(|_| Error::refused("cannot back up the daemon configuration"))?;
        let old =
            fs::read(&path).map_err(|_| Error::refused("cannot read the daemon configuration"))?;
        file.write_all(&old)
            .and_then(|()| file.sync_all())
            .map_err(|_| Error::refused("cannot back up the daemon configuration"))?;
        fs::set_permissions(
            &backup,
            fs::metadata(&path)
                .map_err(|_| Error::refused("cannot read configuration permissions"))?
                .permissions(),
        )
        .map_err(|_| Error::refused("cannot preserve backup permissions"))?;
        Some(backup)
    } else {
        None
    };
    let temporary = parent.join(format!(".config-{}.tmp", uuid::Uuid::new_v4().simple()));
    let written = (|| {
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temporary)
            .map_err(|_| Error::refused("cannot stage the daemon configuration"))?;
        file.write_all(&bytes)
            .and_then(|()| file.write_all(b"\n"))
            .and_then(|()| file.sync_all())
            .map_err(|_| Error::refused("cannot stage the daemon configuration"))?;
        if backup.is_some() {
            fs::set_permissions(
                &temporary,
                fs::metadata(&path)
                    .map_err(|_| Error::refused("cannot read configuration permissions"))?
                    .permissions(),
            )
            .map_err(|_| Error::refused("cannot preserve configuration permissions"))?;
        }
        drop(file);
        fs::rename(&temporary, &path)
            .map_err(|_| Error::refused("cannot replace the daemon configuration"))?;
        Ok(json!({"config_path":path,"backup":backup}))
    })();
    let _ = fs::remove_file(&temporary);
    written
}

/// JSON syntax with YAML 1.1-compatible float spellings. PyYAML resolves
/// `1e20` as a string; `1.0e+20` keeps the same float on both daemon engines.
fn json_yaml(value: &Value) -> String {
    match value {
        Value::Number(number) => {
            let text = number.to_string();
            if let Some((mantissa, exponent)) = text.split_once(['e', 'E']) {
                format!(
                    "{}{}e{}{}",
                    mantissa,
                    if mantissa.contains('.') { "" } else { ".0" },
                    if exponent.starts_with(['+', '-']) {
                        ""
                    } else {
                        "+"
                    },
                    exponent
                )
            } else {
                text
            }
        }
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(json_yaml).collect::<Vec<_>>().join(",")
        ),
        Value::Object(items) => format!(
            "{{{}}}",
            items
                .iter()
                .map(|(key, value)| format!(
                    "{}:{}",
                    json_yaml(&Value::String(key.clone())),
                    json_yaml(value)
                ))
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::String(_) => serde_json::to_string(value)
            .expect("string serialization")
            .chars()
            .map(|c| match c {
                '\u{7f}'..='\u{9f}' | '\u{2028}' | '\u{2029}' | '\u{fffe}' | '\u{ffff}' => {
                    format!("\\u{:04x}", c as u32)
                }
                c => c.to_string(),
            })
            .collect(),
        _ => serde_json::to_string(value).expect("scalar serialization"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn names_follow_browser_origin_serialization() {
        assert_eq!(problem("localhost", "http://localhost"), None);
        assert_eq!(problem("node.invalid", "https://node.invalid:8443"), None);
        for (rp, url, why) in [
            (
                "localhost",
                "http://localhost:80",
                "origin must leave out the default port :80",
            ),
            (
                "node.invalid",
                "http://node.invalid",
                "plain http is allowed for rp_id localhost only",
            ),
            (
                "NODE.invalid",
                "https://NODE.invalid",
                "rp_id must be a lower-case hostname",
            ),
            (
                "localhost",
                "http://localhost/",
                "origin must be exactly <scheme>://<rp_id>[:<port>]",
            ),
        ] {
            assert_eq!(problem(rp, url).as_deref(), Some(why));
        }
    }
    #[test]
    fn yaml11_scalars_preserve_types() {
        for (text, want) in [
            ("yes", Yaml::Boolean(true)),
            ("017", Yaml::Integer(15)),
            ("1_000", Yaml::Integer(1000)),
            ("1:30", Yaml::Integer(90)),
            ("1e5", Yaml::String("1e5".into())),
        ] {
            let mut loader = Loader::default();
            Parser::new_from_str(text).load(&mut loader, true).unwrap();
            assert_eq!(loader.loader.documents(), &[want]);
        }
    }

    #[test]
    fn unbounded_integer_projection_preserves_value_and_type() {
        for (input, expected) in [
            ("18446744073709551616", "18446744073709551616"),
            ("0x10000000000000000", "18446744073709551616"),
            ("02000000000000000000000", "18446744073709551616"),
            ("-18446744073709551616", "-18446744073709551616"),
            ("1:00:00", "3600"),
            ("-0", "0"),
        ] {
            assert_eq!(integer(input).as_deref(), Some(expected));
            let mut loader = Loader {
                reading: true,
                ..Default::default()
            };
            Parser::new_from_str(input).load(&mut loader, true).unwrap();
            let value = json_value(&loader.loader.documents()[0]).unwrap();
            assert!(value.is_number());
            assert_eq!(value.to_string(), expected);
        }
    }

    #[test]
    fn serialized_floats_and_quoted_merge_keys_keep_their_types() {
        let document = json!({"large":1.0e20,"small":1.0e-20,"<<":{"quoted":true}});
        let encoded = json_yaml(&document);
        let mut loader = Loader::default();
        Parser::new_from_str(&encoded)
            .load(&mut loader, true)
            .unwrap();
        assert!(loader.problem.is_none());
        assert_eq!(json_value(&loader.loader.documents()[0]).unwrap(), document);
    }
}
