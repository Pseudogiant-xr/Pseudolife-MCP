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

/// YAML 1.1 scalar resolution through yaml-rust2's event loader. Quoted values
/// stay strings. JSON output is YAML and preserves all represented value types.
#[derive(Default)]
struct Loader {
    loader: YamlLoader,
    problem: Option<String>,
    collection_anchors: std::collections::BTreeSet<usize>,
    shared: bool,
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
                if tag.is_some() {
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
            Event::Scalar(value, style, id, tag) => match scalar(value, style, tag) {
                Ok((value, style, tag)) => Event::Scalar(value, style, id, tag),
                Err(problem) => {
                    self.problem = Some(problem);
                    return;
                }
            },
            _ => event,
        };
        self.loader.on_event(event, mark);
    }
}

fn scalar(
    mut value: String,
    style: TScalarStyle,
    tag: Option<Tag>,
) -> std::result::Result<(String, TScalarStyle, Option<Tag>), String> {
    if tag.is_none() && style == TScalarStyle::Plain && value == "<<" {
        return Err("config.yaml uses YAML merge keys; edit it on the daemon host instead".into());
    }
    if tag.is_none() && style != TScalarStyle::Plain {
        return Ok((value, style, None));
    }
    let explicit = tag.as_ref().map(|t| format!("{}{}", t.handle, t.suffix));
    let tag = explicit.as_deref();
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
        value=integer(&value).ok_or_else(||"config.yaml holds an integer outside the supported range; edit it on the daemon host instead".to_owned())?.to_string();
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
        let number = number
            .filter(|n| n.is_finite())
            .ok_or_else(|| "config.yaml holds an unsupported non-finite number".to_owned())?;
        value = format!("{number:?}");
    } else {
        let bytes = value.as_bytes();
        if tag.is_none()
            && bytes.len() >= 10
            && bytes[4] == b'-'
            && bytes[7] == b'-'
            && bytes[..4].iter().all(u8::is_ascii_digit)
        {
            return Err("config.yaml holds a YAML timestamp this command cannot rewrite; edit it on the daemon host instead".into());
        }
        return Ok((value, TScalarStyle::DoubleQuoted, None));
    }
    Ok((value, TScalarStyle::Plain, None))
}

fn integer(value: &str) -> Option<i64> {
    let digits = value.replace('_', "");
    let (sign, digits) = if let Some(v) = digits.strip_prefix('-') {
        (-1i128, v)
    } else {
        (1, digits.strip_prefix('+').unwrap_or(&digits))
    };
    let magnitude = if let Some(v) = digits.strip_prefix("0x") {
        i128::from_str_radix(v, 16).ok()
    } else if let Some(v) = digits.strip_prefix("0b") {
        i128::from_str_radix(v, 2).ok()
    } else if digits.len() > 1 && digits.starts_with('0') {
        i128::from_str_radix(digits, 8).ok()
    } else if digits.contains(':') && !digits.starts_with('0') {
        digits.split(':').enumerate().try_fold(0i128, |n, (i, p)| {
            let p = p.parse::<i128>().ok()?;
            if i > 0 && p >= 60 {
                None
            } else {
                n.checked_mul(60)?.checked_add(p)
            }
        })
    } else {
        digits.parse::<i128>().ok()
    }?;
    i64::try_from(magnitude.checked_mul(sign)?).ok()
}

fn json_value(value: &Yaml) -> Result<Value> {
    Ok(match value {
        Yaml::Null | Yaml::BadValue => Value::Null,
        Yaml::Boolean(v) => json!(v),
        Yaml::Integer(v) => json!(v),
        Yaml::String(v) => json!(v),
        Yaml::Real(v) => {
            let n = v
                .parse::<f64>()
                .ok()
                .and_then(serde_json::Number::from_f64)
                .ok_or_else(|| {
                    Error::refused("config.yaml holds an unsupported non-finite number")
                })?;
            Value::Number(n)
        }
        Yaml::Array(v) => Value::Array(v.iter().map(json_value).collect::<Result<_>>()?),
        Yaml::Hash(v) => {
            let mut out = serde_json::Map::new();
            for (key, value) in v {
                let key = key
                    .as_str()
                    .ok_or_else(|| Error::refused("config.yaml holds a non-string mapping key"))?;
                out.insert(key.into(), json_value(value)?);
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
    // The ordinary loader first validates structure and duplicate mapping keys.
    let raw = YamlLoader::load_from_str(&text)
        .map_err(|_| Error::refused("cannot read the daemon configuration: invalid YAML"))?;
    let mut loader = Loader::default();
    Parser::new_from_str(&text)
        .load(&mut loader, true)
        .map_err(|_| Error::refused("cannot read the daemon configuration: invalid YAML"))?;
    if let Some(problem) = loader.problem {
        return Err(Error::refused(problem));
    }
    if rewriting && loader.shared {
        return Err(Error::refused(
            "config.yaml shares a mapping or list through a YAML anchor and alias; edit it on the daemon host instead",
        ));
    }
    let docs = loader.loader.documents();
    if docs.len() != raw.len() {
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
        .map(json_value)
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
                    serde_json::to_string(key).expect("string serialization"),
                    json_yaml(value)
                ))
                .collect::<Vec<_>>()
                .join(",")
        ),
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
