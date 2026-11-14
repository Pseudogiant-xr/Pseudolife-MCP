//! Plain refusals for what this client no longer does: open a lite-tier bank
//! (an embedded PostgreSQL cluster in a data directory, retired on 14
//! November 2026), run the in-process `embedded` server (retired with file
//! mode), or run the memory daemon itself (`serve`; the daemon is the
//! separate `pseudolife-daemon` binary in the Docker tier).
//!
//! Every command that finds a lite bank refuses with [`lite_bank_refusal`],
//! so the wording is one sentence from one place. The test for a lite bank is
//! the exact signal the lite tier itself wrote, `embedded_pg/PG_VERSION` as a
//! file: the lite default directory alone is not one, because the native
//! client installs its own runtimes there too.
use std::path::{Path, PathBuf};

/// How the Docker tier is installed, as the README's "Durable tier — Docker"
/// section and the installers name it.
pub const DOCKER_INSTALL: &str =
    "install the Docker tier with ops/install.sh (Linux, macOS) or ops\\install.ps1 (Windows)";
/// How a stopped Docker-tier daemon is started.
pub const DOCKER_START: &str = "docker compose -f ops/docker-compose.yml up -d";

/// Whether `data_dir` holds a lite-tier bank.
pub fn is_lite_bank(data_dir: &Path) -> bool {
    data_dir.join("embedded_pg").join("PG_VERSION").is_file()
}

/// The lite tier's default data directory (`embedded_pg.default_lite_data_dir`):
/// `%LOCALAPPDATA%\pseudolife-mcp`, `~/Library/Application Support/pseudolife-mcp`
/// or `$XDG_DATA_HOME/pseudolife-mcp`. `None` when no home is known.
pub fn lite_default_dir() -> Option<PathBuf> {
    let set = |name: &str| std::env::var_os(name).filter(|value| !value.is_empty());
    let home = || {
        if cfg!(windows) {
            set("USERPROFILE").map(PathBuf::from).or_else(|| {
                let path = set("HOMEPATH")?;
                let mut drive = std::env::var_os("HOMEDRIVE").unwrap_or_default();
                drive.push(path);
                Some(PathBuf::from(drive))
            })
        } else {
            set("HOME").map(PathBuf::from)
        }
    };
    let base = if cfg!(windows) {
        set("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| Some(home()?.join("AppData").join("Local")))?
    } else if cfg!(target_os = "macos") {
        home()?.join("Library").join("Application Support")
    } else {
        set("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| Some(home()?.join(".local").join("share")))?
    };
    Some(base.join("pseudolife-mcp"))
}

/// The lite bank a command without a database URL would have opened: the
/// explicit data directory, else `PSEUDOLIFE_MCP_DATA_DIR`, else the lite
/// default when it exists, else `./data` (`_default_data_dir`'s order);
/// `None` when that directory holds no lite bank.
pub fn find_lite_bank(explicit: Option<&Path>) -> Option<PathBuf> {
    let directory = match explicit.filter(|path| !path.as_os_str().is_empty()) {
        Some(path) => path.to_path_buf(),
        None => match std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR").filter(|v| !v.is_empty()) {
            Some(path) => PathBuf::from(path),
            None => match lite_default_dir().filter(|path| path.is_dir()) {
                Some(path) => path,
                None => std::env::current_dir().ok()?.join("data"),
            },
        },
    };
    is_lite_bank(&directory).then_some(directory)
}

/// The one wording of the refusal every command gives a lite-tier bank,
/// without its line ending. Each command prints it after its own prefix
/// (`lease: break refused: `, `invite: `, `error: `) and exits with the
/// status its other refusals use.
pub fn lite_bank_refusal(data_dir: &Path) -> String {
    format!(
        "{} holds a lite-tier bank (embedded PostgreSQL), and the lite tier is retired, so this client does not open it. Move the bank to the Docker tier: {DOCKER_INSTALL}, then follow \"Move a lite bank to Docker\" in docs/guide/configuration.md.",
        data_dir.display()
    )
}

/// `pseudolife-mcp serve`, without its line ending.
pub fn serve_message() -> String {
    format!(
        "pseudolife-mcp serve: this client does not run the memory daemon. The daemon is the separate pseudolife-daemon binary in the Docker tier: {DOCKER_INSTALL}, then start it with `{DOCKER_START}`."
    )
}

/// `pseudolife-mcp embedded`, without its line ending.
pub fn embedded_message() -> String {
    format!(
        "pseudolife-mcp embedded: the in-process server (no daemon, no PostgreSQL) was retired with file mode. Use the Docker tier instead: {DOCKER_INSTALL}, which registers this machine's clients with its daemon."
    )
}

/// The environment variable that admits the bounded maintainer-testing HTTP
/// candidate (`sent_serve`) behind `serve`; only its test harnesses set it.
pub const SENT_CANDIDATE_ENV: &str = "PSEUDOLIFE_STDIO_SENT_CANDIDATE";

/// Whether `serve` runs the maintainer-testing candidate instead of refusing.
pub fn sent_candidate_requested() -> bool {
    std::env::var_os(SENT_CANDIDATE_ENV).is_some_and(|value| value == "1")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("pl-retired-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&path);
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn a_lite_bank_is_the_marker_file_not_the_directory() {
        let root = scratch("dir");
        assert!(!is_lite_bank(&root));
        std::fs::create_dir_all(root.join("embedded_pg")).unwrap();
        assert!(!is_lite_bank(&root), "an embedded_pg directory alone");
        std::fs::create_dir_all(root.join("embedded_pg").join("PG_VERSION")).unwrap();
        assert!(!is_lite_bank(&root), "PG_VERSION as a directory");
        let file = scratch("file");
        std::fs::create_dir_all(file.join("embedded_pg")).unwrap();
        std::fs::write(file.join("embedded_pg").join("PG_VERSION"), "18\n").unwrap();
        assert!(is_lite_bank(&file));
        for path in [root, file] {
            let _ = std::fs::remove_dir_all(path);
        }
    }

    #[test]
    fn every_refusal_names_the_docker_install() {
        let dir = Path::new("bank");
        for message in [lite_bank_refusal(dir), serve_message(), embedded_message()] {
            assert!(message.contains(DOCKER_INSTALL), "{message}");
            assert!(!message.contains("python"), "{message}");
            assert!(!message.contains("deferred"), "{message}");
        }
        assert!(lite_bank_refusal(dir).starts_with("bank holds a lite-tier bank"));
    }
}
