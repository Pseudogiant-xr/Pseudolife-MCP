//! `connect_cli.discover`: one row per place a selected client keeps the
//! daemon URL, for the JSON-file clients (Claude Code, Claude Desktop,
//! Gemini CLI). A Codex config file, a User-scope daemon URL and an
//! unattended-update schedule defer before discovery (see `mod.rs`).
use super::files::{self, Read};
use super::pyjson::{self, Dict, J};
use super::{Cred, Defer, paths, url};
use super::{
    DEFAULT_URL, DESKTOP_SERVER, FILE_KEY, NO_SPAWN_KEY, SERVER, STATE_KEY, TOKEN_KEY, URL_KEY,
};
use std::path::{Path, PathBuf};

pub(super) struct Ctx {
    pub url: String,
    pub remote: bool,
    pub token_file: Option<String>,
    pub clients: Vec<&'static str>,
    pub layout: Option<paths::Layout>,
}

impl Ctx {
    fn shim(&self) -> Result<String, Defer> {
        match &self.layout {
            Some(layout) if files::is_file(&layout.launcher)? => paths::show(&layout.launcher),
            _ => Ok("<shim path>".to_owned()),
        }
    }
}

pub(super) type Edits = Vec<(String, Option<String>)>;
/// `{key: [old, new]}` as displayed, in key order.
pub(super) type Changes = Vec<(String, Option<String>, Option<String>)>;

pub(super) struct JsonSpec {
    pub path: PathBuf,
    pub pointer: Vec<String>,
    pub edits: Edits,
}

pub(super) struct Row {
    pub client: &'static str,
    pub place: &'static str,
    pub file: Option<String>,
    pub key: Option<String>,
    pub state: &'static str,
    pub changes: Changes,
    pub detail: String,
    pub notes: Vec<String>,
    pub backup: Option<String>,
    pub created: bool,
    pub json: Option<JsonSpec>,
    /// `"_credential" in row`, and its value.
    pub credential: Option<Option<Cred>>,
}

fn row(
    client: &'static str,
    place: &'static str,
    file: Option<&Path>,
    key: Option<&str>,
    state: &'static str,
) -> Result<Row, Defer> {
    Ok(Row {
        client,
        place,
        file: file.map(paths::show).transpose()?,
        key: key.map(str::to_owned),
        state,
        changes: Vec::new(),
        detail: String::new(),
        notes: Vec::new(),
        backup: None,
        created: false,
        json: None,
        credential: None,
    })
}

fn manual(
    client: &'static str,
    place: &'static str,
    file: Option<&Path>,
    key: Option<&str>,
    detail: String,
) -> Result<Row, Defer> {
    let mut found = row(client, place, file, key, "manual")?;
    found.detail = detail;
    Ok(found)
}

impl Row {
    pub(super) fn public(&self) -> J {
        let text = |value: &Option<String>| value.clone().map_or(J::Null, J::Str);
        J::Dict(vec![
            ("client".into(), J::Str(self.client.into())),
            ("place".into(), J::Str(self.place.into())),
            ("file".into(), text(&self.file)),
            ("key".into(), text(&self.key)),
            ("state".into(), J::Str(self.state.into())),
            (
                "changes".into(),
                J::Dict(
                    self.changes
                        .iter()
                        .map(|(key, old, new)| (key.clone(), J::List(vec![text(old), text(new)])))
                        .collect(),
                ),
            ),
            ("detail".into(), J::Str(self.detail.clone())),
            (
                "notes".into(),
                J::List(self.notes.iter().cloned().map(J::Str).collect()),
            ),
            ("backup".into(), text(&self.backup)),
            ("created".into(), J::Bool(self.created)),
        ])
    }
}

/// `d.get(key, default)` keeping an explicit null apart from an absent key.
fn raw<'a>(dict: &'a Dict, key: &str) -> Option<&'a J> {
    dict.iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

fn get_str<'a>(dict: &'a Dict, key: &str) -> Option<&'a str> {
    pyjson::get(dict, key).and_then(J::as_str)
}

fn truthy(dict: &Dict, key: &str) -> bool {
    pyjson::get(dict, key).is_some_and(J::truthy)
}

pub(super) fn after(env: &Dict, edits: &Edits) -> Dict {
    let mut result = env.clone();
    for (key, value) in edits {
        match value {
            None => pyjson::remove(&mut result, key),
            Some(value) => pyjson::set(&mut result, key, J::Str(value.clone())),
        }
    }
    result
}

fn changes(env: &Dict, edits: &Edits) -> Result<Changes, Defer> {
    let mut sorted: Vec<_> = edits.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    let mut shown = Vec::new();
    for (key, new) in sorted {
        let old = pyjson::get(env, key);
        let pair = if key == TOKEN_KEY {
            (
                old.is_some_and(J::truthy)
                    .then(|| "<literal token>".to_owned()),
                new.as_ref().map(|_| "<literal token>".to_owned()),
            )
        } else if key == URL_KEY {
            let old = match old {
                None => None,
                Some(value) => Some(url::shown(&value.py_str()?)?),
            };
            (old, new.clone())
        } else {
            (old.map(J::py_str).transpose()?, new.clone())
        };
        shown.push((key.clone(), pair.0, pair.1));
    }
    Ok(shown)
}

/// `_credential(env)`: the file wins.
fn credential(env: &Dict) -> Result<Option<Cred>, Defer> {
    if let Some(value) = pyjson::get(env, FILE_KEY).filter(|v| v.truthy()) {
        return Ok(Some(Cred::File(value.py_str()?)));
    }
    if let Some(value) = pyjson::get(env, TOKEN_KEY).filter(|v| v.truthy()) {
        return Ok(Some(Cred::Literal(value.py_str()?)));
    }
    Ok(None)
}

fn registration_edits(env: &Dict, ctx: &Ctx) -> Result<Edits, Defer> {
    let mut edits = Vec::new();
    if get_str(env, URL_KEY) != Some(ctx.url.as_str()) {
        edits.push((URL_KEY.to_owned(), Some(ctx.url.clone())));
    }
    if let Some(token_file) = &ctx.token_file {
        if get_str(env, FILE_KEY) != Some(token_file.as_str()) {
            edits.push((FILE_KEY.to_owned(), Some(token_file.clone())));
        }
        if pyjson::has(env, TOKEN_KEY) {
            edits.push((TOKEN_KEY.to_owned(), None));
        }
    }
    if ctx.remote {
        let current = match raw(env, NO_SPAWN_KEY) {
            None => String::new(),
            Some(value) => value.py_str()?,
        };
        let current = pyjson::py_strip(&current).to_ascii_lowercase();
        if !matches!(current.as_str(), "1" | "true" | "yes" | "on") {
            edits.push((NO_SPAWN_KEY.to_owned(), Some("1".to_owned())));
        }
    }
    Ok(edits)
}

fn settings_edits(env: &Dict, ctx: &Ctx, fallback: Option<&str>) -> Edits {
    let mut edits = Vec::new();
    let has_credential = truthy(env, FILE_KEY) || truthy(env, TOKEN_KEY);
    let wanted = ctx
        .token_file
        .as_deref()
        .or(if has_credential { None } else { fallback });
    if let Some(wanted) = wanted.filter(|w| !w.is_empty())
        && get_str(env, FILE_KEY) != Some(wanted)
    {
        edits.push((FILE_KEY.to_owned(), Some(wanted.to_owned())));
    }
    if ctx.token_file.is_some() && pyjson::has(env, TOKEN_KEY) {
        edits.push((TOKEN_KEY.to_owned(), None));
    }
    let file_edited = edits.iter().any(|(key, _)| key == FILE_KEY);
    if get_str(env, URL_KEY) != Some(ctx.url.as_str())
        && (pyjson::has(env, URL_KEY) || ctx.url != DEFAULT_URL || file_edited)
    {
        edits.push((URL_KEY.to_owned(), Some(ctx.url.clone())));
    }
    edits
}

fn notes(env: &Dict, ctx: &Ctx, edits: &Edits, command: Option<&str>) -> Vec<String> {
    let mut found = Vec::new();
    if truthy(&after(env, edits), TOKEN_KEY) {
        found.push(format!(
            "keeps its literal {TOKEN_KEY}: pass --token-file to replace it with an owner-only token file"
        ));
    }
    if truthy(env, STATE_KEY) {
        found.push(format!(
            "sets a fixed {STATE_KEY}, which is not keyed by the daemon URL: the coordination adapter refuses a state bound to another bank; remove it and use PSEUDOLIFE_AGENT_STATE_DIR"
        ));
    }
    if let (Some(command), Some(layout)) = (command, &ctx.layout)
        && !paths::same_path(Path::new(command), &layout.launcher)
    {
        found.push(format!(
            "runs {command}, not the shim launcher; connect leaves the command alone (`pseudolife-mcp update --clients-only`, or `python ops/shim_runtime.py migrate` from a checkout, moves it)"
        ));
    }
    found
}

fn is_shim(entry: &J) -> bool {
    entry.as_dict().is_some_and(|entry| {
        get_str(entry, "command").is_some()
            && matches!(pyjson::get(entry, "type"), None | Some(J::Str(_)))
            && pyjson::get(entry, "type").is_none_or(|kind| kind.as_str() == Some("stdio"))
    })
}

fn register_command(client: &str, ctx: &Ctx) -> Result<String, Defer> {
    let token = ctx.token_file.as_deref().unwrap_or("<token file>");
    let shim = ctx.shim()?;
    let mut pairs = Vec::new();
    if ctx.remote {
        pairs.push(format!("{NO_SPAWN_KEY}=1"));
    }
    pairs.push(format!("{FILE_KEY}={token}"));
    pairs.push(format!("{URL_KEY}={}", ctx.url));
    let flags = |flag: &str, writer: &str| {
        std::iter::once(format!("PSEUDOLIFE_WRITER_ID={writer}"))
            .chain(pairs.iter().cloned())
            .map(|pair| format!("{flag} {pair}"))
            .collect::<Vec<_>>()
            .join(" ")
    };
    Ok(match client {
        "claude-code" => format!(
            "claude mcp add --scope user {SERVER} {} -- {shim}",
            flags("-e", "claude-code")
        ),
        "codex" => format!(
            "codex mcp add {SERVER} {} -- {shim}",
            flags("--env", "codex")
        ),
        "gemini" => format!(
            "gemini mcp add -s user {} {SERVER} {shim}",
            flags("-e", "gemini")
        ),
        _ => format!(
            "python ops/register_claude_desktop.py --command {shim} --daemon-url {} --token-file {token} (from a checkout; the installers run it for --client claude-desktop)",
            ctx.url
        ),
    })
}

fn absent(client: &'static str, ctx: &Ctx) -> Result<Row, Defer> {
    let mut found = row(client, "registration", None, None, "absent")?;
    found.detail = format!(
        "no registration; connect never creates one. To register: {}",
        register_command(client, ctx)?
    );
    Ok(found)
}

fn json_registration(
    ctx: &Ctx,
    client: &'static str,
    path: &Path,
    key: &str,
    entry: &Dict,
) -> Result<Row, Defer> {
    let empty = Vec::new();
    let env = match raw(entry, "env") {
        None | Some(J::Null) => &empty,
        Some(J::Dict(env)) => env,
        Some(_) => {
            return manual(
                client,
                "registration",
                Some(path),
                Some(key),
                format!("mcpServers.{key}.env is not a JSON object; fix it by hand, then re-run"),
            );
        }
    };
    let edits = registration_edits(env, ctx)?;
    let command = get_str(entry, "command").ok_or(Defer)?;
    let state = if edits.is_empty() {
        "current"
    } else {
        "change"
    };
    let mut found = row(client, "registration", Some(path), Some(key), state)?;
    found.changes = changes(env, &edits)?;
    found.notes = notes(env, ctx, &edits, Some(command));
    found.credential = Some(credential(&after(env, &edits))?);
    found.json = Some(JsonSpec {
        path: path.to_path_buf(),
        pointer: vec!["mcpServers".into(), key.into(), "env".into()],
        edits,
    });
    Ok(found)
}

fn non_shim(client: &'static str, path: &Path, key: &str, entry: &J) -> Result<Row, Defer> {
    let kind = entry
        .as_dict()
        .and_then(|entry| pyjson::get(entry, "type"))
        .filter(|kind| kind.truthy())
        .map(J::py_str)
        .transpose()?
        .unwrap_or_else(|| "unknown".to_owned());
    manual(
        client,
        "registration",
        Some(path),
        Some(key),
        format!(
            "not a stdio registration of the shim (type: {kind}); connect rewrites only the shim's own. Re-register it: {SERVER} over stdio"
        ),
    )
}

fn data_of(read: Read) -> (Option<Dict>, Option<String>) {
    match read {
        Read::Missing => (None, None),
        Read::Error(error) => (None, Some(error)),
        Read::Ok(data) => (Some(data), None),
    }
}

fn servers_of(data: &Option<Dict>) -> Option<&Dict> {
    data.as_ref()
        .and_then(|data| pyjson::get(data, "mcpServers"))
        .and_then(J::as_dict)
}

fn claude_rows(ctx: &Ctx) -> Result<Vec<Row>, Defer> {
    let path = paths::claude_config()?;
    let (data, error) = data_of(files::read_json(&path)?);
    if let Some(error) = error {
        return Ok(vec![manual(
            "claude-code",
            "registration",
            Some(&path),
            Some(SERVER),
            error,
        )?]);
    }
    let mut rows = Vec::new();
    if let Some(entry) = servers_of(&data).and_then(|servers| pyjson::get(servers, SERVER)) {
        rows.push(match entry {
            J::Dict(dict) if is_shim(entry) => {
                json_registration(ctx, "claude-code", &path, SERVER, dict)?
            }
            _ => non_shim("claude-code", &path, SERVER, entry)?,
        });
    }
    if let Some(projects) = data
        .as_ref()
        .and_then(|data| pyjson::get(data, "projects"))
        .and_then(J::as_dict)
    {
        for (project, record) in projects {
            let scoped = record
                .as_dict()
                .and_then(|record| pyjson::get(record, "mcpServers"))
                .and_then(J::as_dict);
            if scoped.is_some_and(|scoped| pyjson::has(scoped, SERVER)) {
                rows.push(manual(
                    "claude-code",
                    "project",
                    Some(&path),
                    Some(&format!("projects[\"{project}\"].mcpServers.{SERVER}")),
                    "a project-scoped registration: connect does not rewrite project scopes. Edit its env by hand, or remove it so the user-scope registration applies".to_owned(),
                )?);
            }
        }
    }
    let cwd = std::env::current_dir().map_err(|_| Defer)?;
    let cwd = paths::base(cwd.to_str().ok_or(Defer)?)?;
    let local = cwd.join(".mcp.json");
    let (local_data, local_error) = data_of(files::read_json(&local)?);
    let scoped = servers_of(&local_data).is_some_and(|scoped| pyjson::has(scoped, SERVER));
    if local_error.is_some() || scoped {
        rows.push(manual(
            "claude-code",
            "project",
            Some(&local),
            Some(&format!("mcpServers.{SERVER}")),
            local_error.unwrap_or_else(|| {
                "a project-scoped registration in this directory's .mcp.json: connect does not rewrite project scopes; edit it by hand".to_owned()
            }),
        )?);
    }
    match rows.iter().position(|row| row.place == "registration") {
        None => rows.insert(0, absent("claude-code", ctx)?),
        Some(index) if matches!(rows[index].state, "current" | "change") => {
            let settings = settings_row(ctx, &rows[index])?;
            rows.insert(index + 1, settings);
        }
        Some(_) => {}
    }
    Ok(rows)
}

fn settings_row(ctx: &Ctx, registration: &Row) -> Result<Row, Defer> {
    let path = paths::claude_settings()?;
    let (data, error) = data_of(files::read_json(&path)?);
    if let Some(error) = error {
        return manual("claude-code", "settings", Some(&path), Some("env"), error);
    }
    let empty = Vec::new();
    let env = match data.as_ref().and_then(|data| raw(data, "env")) {
        None | Some(J::Null) => &empty,
        Some(J::Dict(env)) => env,
        Some(_) => {
            return manual(
                "claude-code",
                "settings",
                Some(&path),
                Some("env"),
                "settings.json 'env' is not an object; fix it by hand, then re-run".to_owned(),
            );
        }
    };
    let fallback = match &registration.credential {
        Some(Some(Cred::File(file))) => Some(file.as_str()),
        _ => None,
    };
    let edits = settings_edits(env, ctx, fallback);
    let state = if edits.is_empty() {
        "current"
    } else {
        "change"
    };
    let mut found = row("claude-code", "settings", Some(&path), Some("env"), state)?;
    found.changes = changes(env, &edits)?;
    if truthy(&after(env, &edits), TOKEN_KEY) {
        found.notes = vec![format!(
            "keeps its literal {TOKEN_KEY}: pass --token-file to replace it"
        )];
    }
    found.credential = Some(credential(&after(env, &edits))?);
    found.json = Some(JsonSpec {
        path,
        pointer: vec!["env".into()],
        edits,
    });
    Ok(found)
}

fn desktop_rows(ctx: &Ctx) -> Result<Vec<Row>, Defer> {
    let mut rows = Vec::new();
    for path in paths::desktop_configs()? {
        let (data, error) = data_of(files::read_json(&path)?);
        if let Some(error) = error {
            rows.push(manual(
                "claude-desktop",
                "registration",
                Some(&path),
                Some(DESKTOP_SERVER),
                error,
            )?);
            continue;
        }
        let Some(servers) = servers_of(&data) else {
            continue;
        };
        if let Some(entry) = pyjson::get(servers, DESKTOP_SERVER) {
            rows.push(match entry {
                J::Dict(dict) if is_shim(entry) => {
                    json_registration(ctx, "claude-desktop", &path, DESKTOP_SERVER, dict)?
                }
                _ => non_shim("claude-desktop", &path, DESKTOP_SERVER, entry)?,
            });
        }
        if let Some(legacy) = pyjson::get(servers, SERVER) {
            let own = legacy
                .as_dict()
                .and_then(|legacy| pyjson::get(legacy, "env"))
                .and_then(J::as_dict)
                .is_some_and(|env| get_str(env, "PSEUDOLIFE_WRITER_ID") == Some("claude-desktop"));
            rows.push(manual(
                "claude-desktop",
                "registration",
                Some(&path),
                Some(SERVER),
                if own {
                    format!(
                        "the entry's old name {SERVER}: the Desktop registrar moves it to {DESKTOP_SERVER} (ops/install.* --client claude-desktop, or python ops/register_claude_desktop.py); re-run connect after that"
                    )
                } else {
                    format!(
                        "a {SERVER} entry the Desktop registrar did not write (its env does not set PSEUDOLIFE_WRITER_ID=claude-desktop); left as it is"
                    )
                },
            )?);
        }
    }
    if rows.is_empty() {
        rows.push(absent("claude-desktop", ctx)?);
    }
    Ok(rows)
}

fn gemini_rows(ctx: &Ctx) -> Result<Vec<Row>, Defer> {
    let path = paths::gemini_settings()?;
    let (data, error) = data_of(files::read_json(&path)?);
    if let Some(error) = error {
        return Ok(vec![manual(
            "gemini",
            "registration",
            Some(&path),
            Some(SERVER),
            error,
        )?]);
    }
    let Some(entry) = servers_of(&data).and_then(|servers| pyjson::get(servers, SERVER)) else {
        return Ok(vec![absent("gemini", ctx)?]);
    };
    Ok(vec![match entry {
        J::Dict(dict) if is_shim(entry) => json_registration(ctx, "gemini", &path, SERVER, dict)?,
        _ => non_shim("gemini", &path, SERVER, entry)?,
    }])
}

fn environment_rows(ctx: &Ctx) -> Result<Vec<Row>, Defer> {
    let mut rows = Vec::new();
    if let Some(ambient) = paths::env_truthy(URL_KEY)? {
        let normalised = url::validated(&ambient)?.unwrap_or_else(|()| ambient.clone());
        if normalised != ctx.url {
            rows.push(manual(
                "environment",
                "process",
                None,
                Some(URL_KEY),
                format!(
                    "this shell sets {URL_KEY}={}: sessions started from it, and the plugin hooks, may take it over the registrations. Unset it or set it to {}",
                    url::shown(&ambient)?,
                    ctx.url
                ),
            )?);
        }
    }
    Ok(rows)
}

pub(super) fn discover(ctx: &Ctx) -> Result<Vec<Row>, Defer> {
    let mut rows = Vec::new();
    if ctx.clients.contains(&"claude-code") {
        rows.extend(claude_rows(ctx)?);
    }
    if ctx.clients.contains(&"codex") {
        // A config.toml defers before discovery; without one Codex is absent.
        rows.push(absent("codex", ctx)?);
    }
    if ctx.clients.contains(&"claude-desktop") {
        rows.extend(desktop_rows(ctx)?);
    }
    if ctx.clients.contains(&"gemini") {
        rows.extend(gemini_rows(ctx)?);
    }
    rows.extend(environment_rows(ctx)?);
    Ok(rows)
}
