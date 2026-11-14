//! Conservative registration discovery and transactional, narrowly scoped edits.
use super::*;
use std::collections::BTreeSet;

pub const SERVER: &str = "pseudolife-memory";
#[derive(Clone, Debug)]
pub struct Registration {
    pub client: String,
    pub file: PathBuf,
    pub key: String,
    pub command: String,
    pub args: Vec<String>,
    pub cwd: Option<String>,
    pub env: Value,
}
impl Registration {
    pub fn managed(&self, layout: &Layout) -> bool {
        same_path(Path::new(&self.command), &layout.launcher)
            || runtime_path(Path::new(&self.command), layout).is_some()
    }

    /// Classify installed legacy registrations by their on-disk distribution
    /// records. Never execute an interpreter, pip or pipx to perform a probe.
    pub fn install_kind(&self, layout: &Layout, repo: Option<&Path>) -> &'static str {
        if self.managed(layout) {
            return "managed";
        }
        let command = Path::new(&self.command);
        if repo.is_some_and(|repo| command.starts_with(repo.join(".venv"))) {
            return "editable";
        }
        let stem = command
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let python = stem.starts_with("python")
            && stem[6..].bytes().all(|b| b.is_ascii_digit() || b == b'.')
            && self.args.len() >= 2
            && self.args[0] == "-m"
            && ["pseudolife_memory", "pseudolife_memory.cli"].contains(&self.args[1].as_str());
        if stem != "pseudolife-mcp" && !python {
            return "unmanaged";
        }
        let resolved = if python {
            command.to_owned()
        } else {
            fs::canonicalize(command).unwrap_or_else(|_| command.to_owned())
        };
        let Some(scripts) = resolved.parent() else {
            return "unmanaged";
        };
        let Some(root) = scripts.parent() else {
            return "unmanaged";
        };
        let pipx = if !root.join("pyvenv.cfg").is_file() && !python {
            pipx_copy_root(command)
        } else {
            None
        };
        let root = pipx.as_deref().unwrap_or(root);
        let venv = root.join("pyvenv.cfg").is_file();
        if !venv && (python || !owned_console(command)) {
            return "unknown";
        }
        let mut libraries = if venv {
            vec![root.join("Lib/site-packages")]
        } else {
            user_libraries()
        };
        if venv && let Ok(entries) = fs::read_dir(root.join("lib")) {
            libraries.extend(
                entries
                    .filter_map(|e| e.ok())
                    .filter(|e| e.file_name().to_string_lossy().starts_with("python"))
                    .map(|e| e.path().join("site-packages")),
            );
        }
        let mut installed = false;
        for library in libraries.into_iter().filter(|p| p.is_dir()) {
            let Ok(entries) = fs::read_dir(&library) else {
                return "unknown";
            };
            for entry in entries {
                let Ok(entry) = entry else {
                    return "unknown";
                };
                let name = entry.file_name().to_string_lossy().into_owned();
                if name.ends_with(".egg-link") || name.starts_with("__editable__") {
                    return "editable";
                }
                if name.ends_with(".pth") {
                    let Ok(text) = fs::read_to_string(entry.path()) else {
                        return "unknown";
                    };
                    for line in text
                        .lines()
                        .map(str::trim)
                        .filter(|l| !l.is_empty() && !l.starts_with('#'))
                    {
                        if line.starts_with("import ") || line.starts_with("import\t") {
                            // setuptools' own distutils hook does not redirect
                            // this package; every other executable .pth is unknown.
                            if name != "distutils-precedence.pth" {
                                return "unknown";
                            }
                        } else if library.join(line).join("pseudolife_memory").exists() {
                            return "editable";
                        }
                    }
                }
                if name.starts_with("pseudolife_mcp-") && name.ends_with(".dist-info") {
                    match read_optional(&entry.path().join("direct_url.json")) {
                        Ok(Some(bytes)) => match json_bytes(&bytes) {
                            Ok(value)
                                if value
                                    .get("dir_info")
                                    .and_then(|v| v.get("editable"))
                                    .and_then(Value::as_bool)
                                    == Some(true) =>
                            {
                                return "editable";
                            }
                            Ok(_) => {}
                            Err(_) => return "unknown",
                        },
                        Ok(None) => {}
                        Err(_) => return "unknown",
                    }
                    installed = true;
                }
            }
            if library.join("pseudolife_memory").is_symlink() {
                return "editable";
            }
        }
        if installed {
            if self.spawns_daemon() {
                "spawning"
            } else {
                "legacy"
            }
        } else {
            "unknown"
        }
    }
    pub fn spawns_daemon(&self) -> bool {
        let no_spawn = self
            .env
            .get("PSEUDOLIFE_MCP_NO_SPAWN")
            .and_then(Value::as_str)
            .unwrap_or("");
        if ["1", "true", "yes", "on"].contains(&no_spawn.trim().to_ascii_lowercase().as_str()) {
            return false;
        }
        let url = self
            .env
            .get("PSEUDOLIFE_MCP_DAEMON_URL")
            .and_then(Value::as_str)
            .unwrap_or("");
        if url.trim().is_empty() {
            return true;
        }
        reqwest::Url::parse(url)
            .ok()
            .and_then(|u| u.host_str().map(str::to_owned))
            .is_none_or(|h| ["localhost", "127.0.0.1", "[::1]", "::1"].contains(&h.as_str()))
    }
}

fn pipx_copy_root(command: &Path) -> Option<PathBuf> {
    let roots = [
        std::env::var_os("PIPX_HOME").map(PathBuf::from),
        Some(home().join(".local/share/pipx")),
        Some(home().join(".local/pipx")),
        Some(home().join("pipx")),
    ];
    for root in roots.into_iter().flatten() {
        let root = root.join("venvs/pseudolife-mcp");
        let metadata = read_optional(&root.join("pipx_metadata.json"))
            .ok()
            .flatten()
            .and_then(|b| json_bytes(&b).ok());
        if metadata
            .as_ref()
            .and_then(|v| v.get("main_package"))
            .and_then(|v| v.get("package"))
            .and_then(Value::as_str)
            != Some("pseudolife-mcp")
        {
            continue;
        }
        let binary = root
            .join(if cfg!(windows) { "Scripts" } else { "bin" })
            .join(BINARY);
        if plain_file(command)
            && plain_file(&binary)
            && hash_file(command)
                .ok()
                .is_some_and(|digest| hash_file(&binary).ok() == Some(digest))
        {
            return Some(root);
        }
    }
    None
}
fn owned_console(path: &Path) -> bool {
    let Ok(file) = fs::File::open(path) else {
        return false;
    };
    let mut bytes = Vec::new();
    if file
        .take(8 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() > 8 * 1024 * 1024
    {
        return false;
    }
    fn entry(text: &str) -> bool {
        text.lines()
            .any(|line| line.trim() == "from pseudolife_memory.cli import main")
    }
    if let Ok(text) = std::str::from_utf8(&bytes)
        && text.starts_with("#!")
        && entry(text)
    {
        return true;
    }
    // distlib appends a ZIP containing __main__.py to its Windows stub.
    let Ok(mut archive) = zip::ZipArchive::new(std::io::Cursor::new(bytes)) else {
        return false;
    };
    let Ok(script) = archive.by_name("__main__.py") else {
        return false;
    };
    let mut text = String::new();
    script.take(65537).read_to_string(&mut text).is_ok() && text.len() <= 65536 && entry(&text)
}
fn user_libraries() -> Vec<PathBuf> {
    let base = std::env::var_os("PYTHONUSERBASE")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            if cfg!(windows) {
                std::env::var_os("APPDATA")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| home().join("AppData/Roaming"))
                    .join("Python")
            } else {
                home().join(".local")
            }
        });
    let parent = if cfg!(windows) {
        base
    } else {
        base.join("lib")
    };
    let Ok(entries) = fs::read_dir(parent) else {
        return vec![];
    };
    entries
        .filter_map(|e| e.ok())
        .filter(|e| {
            e.file_name()
                .to_string_lossy()
                .to_ascii_lowercase()
                .starts_with("python")
        })
        .map(|e| e.path().join("site-packages"))
        .collect()
}
pub fn runtime_path(command: &Path, layout: &Layout) -> Option<PathBuf> {
    let command = fs::canonicalize(command).unwrap_or_else(|_| command.to_owned());
    // A saved dependency may no longer exist. Compare its existing ancestors
    // too: Windows canonicalization adds a verbatim prefix only to paths that
    // exist, so strip_prefix would silently lose a missing dependency's pin.
    let mut child = command.as_path();
    for parent in command.ancestors().skip(1) {
        if same_path(parent, &layout.root) {
            let name = child.file_name()?.to_str()?;
            sequence(name)?;
            return Some(layout.root.join(name));
        }
        child = parent;
    }
    None
}
pub fn codex_home() -> PathBuf {
    std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".codex"))
}
pub fn claude_config_dir() -> PathBuf {
    std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".claude"))
}
pub fn desktop_files() -> Result<Vec<PathBuf>> {
    #[cfg(windows)]
    {
        let mut files = Vec::new();
        if let Some(local) = std::env::var_os("LOCALAPPDATA") {
            let root = PathBuf::from(local).join("Packages");
            if root.exists() {
                for entry in fs::read_dir(root).map_err(io_error)? {
                    let entry = entry.map_err(io_error)?;
                    if entry.file_name().to_string_lossy().starts_with("Claude_") {
                        files.push(
                            entry
                                .path()
                                .join("LocalCache/Roaming/Claude/claude_desktop_config.json"),
                        );
                    }
                }
            }
        }
        files.push(
            std::env::var_os("APPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|| home().join("AppData/Roaming"))
                .join("Claude/claude_desktop_config.json"),
        );
        Ok(files)
    }
    #[cfg(target_os = "macos")]
    {
        Ok(vec![home().join(
            "Library/Application Support/Claude/claude_desktop_config.json",
        )])
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        Ok(vec![
            std::env::var_os("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| home().join(".config"))
                .join("Claude/claude_desktop_config.json"),
        ])
    }
}
pub fn read_optional(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::File::open(path) {
        Ok(file) => {
            if !file.metadata().map_err(io_error)?.is_file() {
                return Err("client configuration is not a regular file".into());
            }
            let mut bytes = Vec::new();
            file.take(16 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)
                .map_err(io_error)?;
            if bytes.len() > 16 * 1024 * 1024 {
                return Err("client configuration exceeds size limit".into());
            }
            Ok(Some(bytes))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound && fs::symlink_metadata(path).is_err() => {
            Ok(None)
        }
        Err(_) => Err("client configuration could not be read; nothing was changed".into()),
    }
}
pub fn json_bytes(bytes: &[u8]) -> Result<Value> {
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    serde_json::from_slice(bytes).map_err(|_| "client configuration is not valid JSON".into())
}
fn json_registrations(
    client: &str,
    path: PathBuf,
    keys: &[&str],
    found: &mut Vec<Registration>,
) -> Result<()> {
    let Some(bytes) = read_optional(&path)? else {
        return Ok(());
    };
    let data = json_bytes(&bytes)?;
    if !data.is_object() {
        return Err("client configuration must be a JSON object".into());
    }
    let Some(servers) = data.get("mcpServers") else {
        return Ok(());
    };
    let servers = servers.as_object().ok_or("mcpServers must be an object")?;
    for key in keys {
        if let Some(entry) = servers.get(*key) {
            add_registration(client, &path, key, entry, found)?;
        }
    }
    Ok(())
}
fn add_registration(
    client: &str,
    path: &Path,
    key: &str,
    entry: &Value,
    found: &mut Vec<Registration>,
) -> Result<()> {
    if !entry.is_object() {
        return Err("client server entry must be an object".into());
    }
    let Some(command) = entry.get("command") else {
        return Ok(());
    }; // HTTP registration.
    let command = command.as_str().ok_or("client command must be a string")?;
    let args = match entry.get("args") {
        None => vec![],
        Some(args) => args
            .as_array()
            .ok_or("client args must be an array")?
            .iter()
            .map(|arg| {
                arg.as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| "client args must contain strings".to_owned())
            })
            .collect::<Result<Vec<_>>>()?,
    };
    found.push(Registration {
        client: client.into(),
        file: path.into(),
        key: key.into(),
        command: command.into(),
        args,
        cwd: entry.get("cwd").and_then(Value::as_str).map(str::to_owned),
        env: entry.get("env").cloned().unwrap_or_else(|| json!({})),
    });
    Ok(())
}
pub fn find_registrations() -> Result<Vec<Registration>> {
    find_selected_registrations(|_| true)
}
/// As `find_registrations`, reading only the files of the clients `selected`
/// admits, so an unreadable configuration of another client cannot block them.
pub fn find_selected_registrations(selected: impl Fn(&str) -> bool) -> Result<Vec<Registration>> {
    let mut found = Vec::new();
    if selected("claude-code") {
        let claude = std::env::var_os("CLAUDE_CONFIG_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(home)
            .join(".claude.json");
        json_registrations("claude-code", claude, &[SERVER], &mut found)?;
    }
    if selected("claude-desktop") {
        for path in desktop_files()? {
            json_registrations(
                "claude-desktop",
                path,
                &["pseudolife-desktop", SERVER],
                &mut found,
            )?;
        }
    }
    if selected("gemini") {
        json_registrations(
            "gemini",
            home().join(".gemini/settings.json"),
            &[SERVER],
            &mut found,
        )?;
    }
    let path = codex_home().join("config.toml");
    if !selected("codex") {
        return Ok(found);
    }
    if let Some(bytes) = read_optional(&path)? {
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| "Codex config is not UTF-8")?
            .trim_start_matches('\u{feff}');
        let data: toml::Value =
            toml::from_str(text).map_err(|_| "Codex config is not valid TOML")?;
        if let Some(entry) = data.get("mcp_servers").and_then(|v| v.get(SERVER)) {
            let value =
                serde_json::to_value(entry).map_err(|_| "Codex server table cannot be read")?;
            add_registration("codex", &path, SERVER, &value, &mut found)?;
        }
    }
    Ok(found)
}
pub fn pinned(layout: &Layout, registrations: &[Registration]) -> BTreeSet<PathBuf> {
    registrations
        .iter()
        .filter_map(|r| runtime_path(Path::new(&r.command), layout))
        .collect()
}
pub fn migrated_args(args: &[String]) -> &[String] {
    if args.len() >= 2
        && args[0] == "-m"
        && ["pseudolife_memory", "pseudolife_memory.cli"].contains(&args[1].as_str())
    {
        &args[2..]
    } else {
        args
    }
}

pub use super::config_file::write_config;

pub fn migrate(registration: &Registration, layout: &Layout) -> Value {
    let result = (|| -> Result<Value> {
        let args = migrated_args(&registration.args);
        if same_path(Path::new(&registration.command), &layout.launcher)
            && args == registration.args
            && registration.cwd.is_none()
        {
            return Ok(
                json!({"state":"current","detail":format!("{}: already runs the launcher",registration.client),"backup":null}),
            );
        }
        let before = read_optional(&registration.file)?.ok_or("client registration disappeared")?;
        let after = if registration.client == "codex" {
            let text = std::str::from_utf8(&before).map_err(|_| "Codex config is not UTF-8")?;
            let parsed: toml::Value =
                toml::from_str(text).map_err(|_| "Codex config is not valid TOML")?;
            let entry = parsed
                .get("mcp_servers")
                .and_then(|v| v.get(SERVER))
                .ok_or("client registration disappeared")?;
            verify_registration(
                &serde_json::to_value(entry).map_err(|e| e.to_string())?,
                registration,
            )?;
            migrate_codex(text, &layout.launcher, args)?
        } else {
            let mut data = json_bytes(&before)?;
            let entry = data
                .get_mut("mcpServers")
                .and_then(|v| v.get_mut(&registration.key))
                .and_then(Value::as_object_mut)
                .ok_or("client registration disappeared")?;
            verify_registration(&Value::Object(entry.clone()), registration)?;
            entry.insert("command".into(), json!(layout.launcher));
            entry.insert("args".into(), json!(args));
            entry.remove("cwd");
            serde_json::to_vec_pretty(&data).map_err(|e| e.to_string())?
        };
        let backup = write_config(&registration.file, &before, &after)?;
        Ok(
            json!({"state":"migrated","detail":format!("{}: now runs the stable native launcher",registration.client),"backup":backup}),
        )
    })();
    let mut result =
        result.unwrap_or_else(|detail| json!({"state":"failed","detail":detail,"backup":null}));
    result["client"] = json!(registration.client);
    result["file"] = json!(registration.file);
    result
}

fn verify_registration(entry: &Value, previous: &Registration) -> Result<()> {
    let mut fresh = Vec::new();
    add_registration(
        &previous.client,
        &previous.file,
        &previous.key,
        entry,
        &mut fresh,
    )?;
    if !fresh.first().is_some_and(|current| {
        current.command == previous.command
            && current.args == previous.args
            && current.cwd == previous.cwd
            && current.env == previous.env
    }) {
        return Err("client registration changed; left as it is".into());
    }
    Ok(())
}

fn migrate_codex(text: &str, launcher: &Path, args: &[String]) -> Result<Vec<u8>> {
    let before: toml::Value = toml::from_str(text).map_err(|_| "Codex config is not valid TOML")?;
    let mut expected = before.clone();
    let entry = expected
        .get_mut("mcp_servers")
        .and_then(|v| v.get_mut(SERVER))
        .and_then(toml::Value::as_table_mut)
        .ok_or("Codex server table disappeared")?;
    entry.insert(
        "command".into(),
        toml::Value::String(launcher.to_string_lossy().into_owned()),
    );
    if entry.contains_key("args") {
        entry.insert(
            "args".into(),
            toml::Value::Array(args.iter().cloned().map(toml::Value::String).collect()),
        );
    }
    entry.remove("cwd");
    let mut output = String::new();
    let mut inside = false;
    let mut saw = false;
    for line in text.split_inclusive('\n') {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            let header = trimmed
                .split('#')
                .next()
                .unwrap_or("")
                .trim()
                .replace(['"', '\''], "");
            inside = header == format!("[mcp_servers.{SERVER}]");
        }
        let key = trimmed.split_once('=').map(|(k, _)| k.trim());
        if inside && matches!(key, Some("command" | "args" | "cwd")) {
            let newline = if line.ends_with("\r\n") { "\r\n" } else { "\n" };
            match key.unwrap() {
                "command" => {
                    output.push_str(&format!(
                        "command = {}{newline}",
                        serde_json::to_string(&launcher.to_string_lossy()).unwrap()
                    ));
                    saw = true;
                }
                "args" => output.push_str(&format!(
                    "args = {}{newline}",
                    serde_json::to_string(args).unwrap()
                )),
                _ => {}
            }
        } else {
            output.push_str(line);
        }
    }
    let after: toml::Value = toml::from_str(&output)
        .map_err(|_| "Codex table edit could not be verified; left as it is")?;
    if !saw || after != expected {
        return Err("Codex config uses a table form this editor cannot isolate; rerun native client setup to migrate it".into());
    }
    Ok(output.into_bytes())
}

/// `name` in an absolute `PATH` directory, never the current directory:
/// `program_lookup.find_program`'s rule (PATHEXT's launchable extensions in
/// its order on Windows). An entry that is not UTF-8 is still searched.
pub fn absolute_command(name: &str) -> Option<PathBuf> {
    crate::cli::program_lookup::find_program_path(name)
}
pub fn expose_launcher(layout: &Layout) -> Value {
    #[cfg(unix)]
    {
        let Some(bin) = &layout.user_bin else {
            return json!({"state":"skipped","detail":"no user bin directory for this layout"});
        };
        let link = bin.join(BINARY);
        if same_path(&link, &layout.launcher) {
            return json!({"state":"current","detail":"user bin already reaches the stable launcher"});
        }
        if fs::symlink_metadata(&link).is_ok() {
            return json!({"state":"left","detail":"user bin entry is already occupied; run the stable launcher by its full path or rerun native setup"});
        }
        let result = fs::create_dir_all(bin)
            .and_then(|_| std::os::unix::fs::symlink(&layout.launcher, &link));
        match result {
            Ok(()) => json!({"state":"linked","detail":"linked user bin to the stable launcher"}),
            Err(_) => {
                json!({"state":"failed","detail":"could not create the user bin link; run the stable launcher by its full path"})
            }
        }
    }
    #[cfg(windows)]
    {
        if !layout.user_path {
            return json!({"state":"skipped","detail":"user PATH is not managed for this layout"});
        }
        // Native installer owns persistent PATH exposure. Registrations always
        // carry an absolute launcher path, so this update never needs a shell.
        json!({"state":"current","detail":"client registrations use the absolute stable launcher; rerun native setup to expose it on user PATH"})
    }
}
