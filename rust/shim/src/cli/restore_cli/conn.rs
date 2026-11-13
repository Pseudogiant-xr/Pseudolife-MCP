//! `update_cli._backup_connection_values`, as `DockerBank.__init__` uses it:
//! every exception is caught there, so only success (and its values) or
//! failure matters. The daemon's `PSEUDOLIFE_MCP_DATABASE_URL` is parsed by
//! libpq through psycopg's `conninfo_to_dict`; this leaf reads the subset of
//! spellings whose libpq parse it is sure of and is `Unsure` otherwise.
use std::collections::BTreeMap;

use serde_json::Value;

use super::{args::python_int, dump::python_space};

#[derive(Debug, PartialEq, Eq)]
pub(super) enum Resolved {
    Ok {
        user: String,
        db: String,
        port: i128,
    },
    Fail,
    Unsure,
}

/// Keywords every supported libpq accepts; another keyword may be valid in
/// one libpq and an error in another.
const KEYWORDS: [&str; 12] = [
    "host",
    "hostaddr",
    "port",
    "dbname",
    "user",
    "password",
    "sslmode",
    "connect_timeout",
    "application_name",
    "options",
    "service",
    "target_session_attrs",
];

/// `conninfo_to_dict(raw)`: `Some(Ok(fields))`, `Some(Err(()))` for a
/// certain parse error, `None` when unsure.
fn conninfo(raw: &str) -> Option<Result<BTreeMap<String, String>, ()>> {
    let mut fields = BTreeMap::new();
    let rest = raw
        .strip_prefix("postgresql://")
        .or_else(|| raw.strip_prefix("postgres://"));
    if let Some(rest) = rest {
        // libpq's URI parser: no query, percent escape, IPv6 literal or host
        // list in the spellings read here.
        if rest.contains(['?', '%', '[', ']', ',']) {
            return None;
        }
        let mut rest = rest;
        if let Some(at) = rest.find(['@', '/'])
            && rest.as_bytes()[at] == b'@'
        {
            let userinfo = &rest[..at];
            let user = userinfo.split(':').next().unwrap_or("");
            if !user.is_empty() {
                fields.insert("user".to_owned(), user.to_owned());
            }
            if let Some((_, password)) = userinfo.split_once(':')
                && !password.is_empty()
            {
                fields.insert("password".to_owned(), password.to_owned());
            }
            rest = &rest[at + 1..];
        }
        let (netloc, dbname) = match rest.split_once('/') {
            Some((netloc, dbname)) => (netloc, Some(dbname)),
            None => (rest, None),
        };
        let (host, port) = match netloc.split_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (netloc, None),
        };
        if !host.is_empty() {
            fields.insert("host".to_owned(), host.to_owned());
        }
        if let Some(port) = port
            && !port.is_empty()
        {
            fields.insert("port".to_owned(), port.to_owned());
        }
        if let Some(dbname) = dbname
            && !dbname.is_empty()
        {
            fields.insert("dbname".to_owned(), dbname.to_owned());
        }
        return Some(Ok(fields));
    }
    // Keyword/value form: plain `key=value` words separated by C `isspace`.
    for word in raw.split([' ', '\t', '\n', '\x0b', '\x0c', '\r']) {
        if word.is_empty() {
            continue;
        }
        let (key, value) = word.split_once('=')?;
        if !KEYWORDS.contains(&key) || value.contains(['\'', '\\']) {
            return None;
        }
        fields.insert(key.to_owned(), value.to_owned());
    }
    if !raw.is_ascii() {
        return None;
    }
    Some(Ok(fields))
}

/// `ipaddress.ip_address(text)` for dotted IPv4 text: `Some(Ok(octets))`,
/// `Some(Err(()))` for a certain ValueError, `None` for any other spelling.
fn ipv4(text: &str) -> Option<Result<[u8; 4], ()>> {
    if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
        return None;
    }
    let parts: Vec<&str> = text.split('.').collect();
    if parts.len() != 4 {
        // Not IPv4; IPv6 also rejects text of digits and dots only.
        return Some(Err(()));
    }
    let mut octets = [0u8; 4];
    for (slot, part) in octets.iter_mut().zip(parts) {
        if part.is_empty() || part.len() > 3 || (part.len() > 1 && part.starts_with('0')) {
            return Some(Err(()));
        }
        match part.parse::<u16>() {
            Ok(value) if value <= 255 => *slot = value as u8,
            _ => return Some(Err(())),
        }
    }
    Some(Ok(octets))
}

/// A JSON value that is a dict in Python; anything else fails `.get`/`[]`.
fn object(value: &Value) -> Option<&serde_json::Map<String, Value>> {
    value.as_object()
}

/// Python truthiness of a JSON value.
pub(super) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// The resolution; `daemon` and `postgres` are the inspected dicts.
pub(super) fn resolve(daemon: &Value, postgres: &Value) -> Resolved {
    match values(daemon, postgres) {
        Some(Some(found)) => found,
        Some(None) => Resolved::Fail,
        None => Resolved::Unsure,
    }
}

/// `None` unsure; `Some(None)` an exception; `Some(Some(_))` the values.
fn values(daemon: &Value, postgres: &Value) -> Option<Option<Resolved>> {
    let fail = Some(None);
    let Some(config) = daemon.get("Config").and_then(object) else {
        return fail;
    };
    let env_list = match config.get("Env") {
        Some(Value::Array(items)) => items,
        // A string or a dict iterates; this leaf does not read those.
        Some(Value::String(_) | Value::Object(_)) => return None,
        _ => return fail,
    };
    let mut env: BTreeMap<&str, &str> = BTreeMap::new();
    for item in env_list {
        let Some(item) = item.as_str() else {
            return fail;
        };
        let Some((key, value)) = item.split_once('=') else {
            return fail;
        };
        if env.insert(key, value).is_some() {
            return fail;
        }
    }
    let Some(raw) = env
        .get("PSEUDOLIFE_MCP_DATABASE_URL")
        .filter(|raw| !raw.is_empty())
    else {
        return fail;
    };
    let parts = match conninfo(raw)? {
        Ok(parts) => parts,
        Err(()) => return fail,
    };
    let setting = |key: &str, variable: &str| -> Option<String> {
        match parts.get(key) {
            Some(value) => Some(value.clone()),
            None => env.get(variable).map(|v| (*v).to_owned()),
        }
    };
    let or = |value: Option<String>, default: &str| -> String {
        value
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| default.to_owned())
    };
    if setting("service", "PGSERVICE").is_some_and(|v| !v.is_empty()) {
        return fail;
    }
    let host = or(setting("host", "PGHOST"), "");
    let address = or(setting("hostaddr", "PGHOSTADDR"), "");
    let port = match python_int(&or(setting("port", "PGPORT"), "5432"))? {
        Ok(port) => port,
        Err(()) => return fail,
    };
    let runtime_user = match config.get("User") {
        None | Some(Value::Null) => None,
        Some(Value::String(user)) => {
            let name = user.split(':').next().unwrap_or("");
            let name = if name.is_empty() { "root" } else { name };
            if !name.is_ascii() {
                return None; // str.isdecimal reads Unicode digits
            }
            if name.bytes().all(|b| b.is_ascii_digit()) {
                None
            } else {
                Some(name.to_owned())
            }
        }
        Some(_) => return fail,
    };
    let user = setting("user", "PGUSER")
        .filter(|v| !v.is_empty())
        .or(runtime_user);
    let Some(user) = user else {
        return fail;
    };
    let db = or(setting("dbname", "PGDATABASE"), &user);
    if user.is_empty()
        || db.is_empty()
        || !(1..=65535).contains(&port)
        || user.chars().chain(db.chars()).any(|c| (c as u32) < 32)
        || host.contains(',')
        || address.contains(',')
    {
        return fail;
    }
    let networks = |inspected: &Value| {
        inspected
            .get("NetworkSettings")
            .and_then(object)
            .and_then(|settings| settings.get("Networks"))
            .and_then(object)
            .cloned()
    };
    let (Some(daemon_networks), Some(pg_networks)) = (networks(daemon), networks(postgres)) else {
        return fail;
    };
    let mut aliases: Vec<String> = Vec::new();
    let mut addresses: Vec<[u8; 4]> = Vec::new();
    for (name, network) in &pg_networks {
        if !daemon_networks.contains_key(name) {
            continue;
        }
        let Some(network) = object(network) else {
            return fail;
        };
        for key in ["Aliases", "DNSNames"] {
            match network.get(key) {
                None => {}
                Some(value) if !truthy(value) => {}
                Some(Value::Array(names)) => {
                    for name in names {
                        match name {
                            Value::String(name) => aliases.push(name.clone()),
                            _ => return None,
                        }
                    }
                }
                Some(_) => return None,
            }
        }
        for key in ["IPAddress", "GlobalIPv6Address"] {
            match network.get(key) {
                None => {}
                Some(value) if !truthy(value) => {}
                Some(Value::String(text)) => match ipv4(text)? {
                    Ok(octets) => {
                        addresses.push(octets);
                        aliases.push(text.clone());
                    }
                    Err(()) => return fail,
                },
                Some(_) => return None,
            }
        }
        match postgres.get("Name") {
            None => aliases.push(String::new()),
            Some(Value::String(name)) => aliases.push(name.trim_start_matches('/').to_owned()),
            Some(_) => return fail,
        }
    }
    if !address.is_empty() {
        match ipv4(&address)? {
            Ok(octets) if addresses.contains(&octets) => {}
            _ => return fail,
        }
    } else if host.is_empty() || !aliases.contains(&host) {
        return fail;
    }
    Some(Some(Resolved::Ok { user, db, port }))
}

/// `psycopg.conninfo.make_conninfo(dbname=database)`: `_param_escape`.
pub(super) fn database_arg(database: &str) -> String {
    if database.is_empty() {
        return "dbname=''".to_owned();
    }
    let escaped = database.replace('\\', "\\\\").replace('\'', "\\'");
    if escaped.chars().any(python_space) {
        format!("dbname='{escaped}'")
    } else {
        format!("dbname={escaped}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn daemon(url: &str) -> Value {
        json!({
            "Config": {"Env": [format!("PSEUDOLIFE_MCP_DATABASE_URL={url}"), "PATH=/bin"], "User": "pseudolife"},
            "NetworkSettings": {"Networks": {"stack": {"IPAddress": "10.0.0.3"}}}
        })
    }

    fn postgres() -> Value {
        json!({
            "Name": "/pseudolife-mcp-postgres",
            "NetworkSettings": {"Networks": {"stack": {"Aliases": ["pseudolife-pg"], "IPAddress": "10.0.0.2"}}}
        })
    }

    #[test]
    fn the_compose_url_resolves() {
        assert_eq!(
            resolve(
                &daemon("postgresql://pseudolife:secret@pseudolife-pg:5432/pseudolife_memory"),
                &postgres()
            ),
            Resolved::Ok {
                user: "pseudolife".into(),
                db: "pseudolife_memory".into(),
                port: 5432
            }
        );
        // The database defaults to the user; the port to 5432.
        assert_eq!(
            resolve(&daemon("postgres://bank@10.0.0.2"), &postgres()),
            Resolved::Ok {
                user: "bank".into(),
                db: "bank".into(),
                port: 5432
            }
        );
        assert_eq!(
            resolve(
                &daemon("host=pseudolife-mcp-postgres port=5433 dbname=b"),
                &postgres()
            ),
            Resolved::Ok {
                user: "pseudolife".into(),
                db: "b".into(),
                port: 5433
            }
        );
    }

    #[test]
    fn failures_and_unsure_spellings() {
        for url in [
            "postgresql://u@elsewhere/db",
            "postgresql://u@pseudolife-pg:0/db",
            "postgresql://u@pseudolife-pg:x/db",
            "host=pseudolife-pg service=s",
            "postgresql://u@/db",
        ] {
            assert_eq!(resolve(&daemon(url), &postgres()), Resolved::Fail, "{url}");
        }
        for url in [
            "postgresql://u@pseudolife-pg/db?sslmode=disable",
            "postgresql://u%40x@pseudolife-pg/db",
            "postgresql://u@[::1]/db",
            "host = pseudolife-pg",
            "host='pseudolife-pg'",
            "unknown=1",
        ] {
            assert_eq!(
                resolve(&daemon(url), &postgres()),
                Resolved::Unsure,
                "{url}"
            );
        }
        let mut odd = daemon("postgresql://u@pseudolife-pg/db");
        odd["Config"]["Env"] = json!("A=B");
        assert_eq!(resolve(&odd, &postgres()), Resolved::Unsure);
        odd["Config"]["Env"] = json!(null);
        assert_eq!(resolve(&odd, &postgres()), Resolved::Fail);
    }

    #[test]
    fn hostaddr_must_match_a_shared_address() {
        assert!(matches!(
            resolve(&daemon("hostaddr=10.0.0.2 user=u"), &postgres()),
            Resolved::Ok { .. }
        ));
        assert_eq!(
            resolve(&daemon("hostaddr=10.0.0.9 user=u"), &postgres()),
            Resolved::Fail
        );
        assert_eq!(
            resolve(&daemon("hostaddr=010.0.0.2 user=u"), &postgres()),
            Resolved::Fail
        );
        assert_eq!(
            resolve(&daemon("hostaddr=fe80::1 user=u"), &postgres()),
            Resolved::Unsure
        );
    }

    #[test]
    fn database_arg_matches_psycopg() {
        assert_eq!(database_arg("postgres"), "dbname=postgres");
        assert_eq!(database_arg(""), "dbname=''");
        assert_eq!(database_arg("a b"), "dbname='a b'");
        assert_eq!(database_arg("o'k\\"), "dbname=o\\'k\\\\");
        assert_eq!(database_arg("a\u{3000}b"), "dbname='a\u{3000}b'");
    }
}
