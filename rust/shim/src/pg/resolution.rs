//! Pure resolution/transport contracts; embedded start/stop is not implemented.
use super::{Dsn, Error};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
};

pub enum Target {
    Direct(Box<Dsn>),
    ExistingEmbedded(PathBuf),
    Container,
}
/// Preserve DSN -> existing embedded -> container order, never initialize a bank.
pub fn select(
    dsn: Option<&str>,
    data_dir: &Path,
    embedded_available: bool,
) -> Result<Target, Error> {
    if let Some(dsn) = dsn.filter(|dsn| !dsn.is_empty()) {
        return Dsn::parse(dsn).map(Box::new).map(Target::Direct);
    }
    let pgdata = data_dir.join("embedded_pg");
    Ok(
        if embedded_available && pgdata.join("PG_VERSION").exists() {
            Target::ExistingEmbedded(pgdata)
        } else {
            Target::Container
        },
    )
}
pub fn data_directory(
    explicit: Option<&Path>,
    lite_default: &Path,
    cwd: &Path,
    embedded_available: bool,
) -> PathBuf {
    explicit
        .filter(|path| !path.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| {
            if embedded_available && lite_default.exists() {
                lite_default.to_path_buf()
            } else {
                cwd.join("data")
            }
        })
}
pub fn container_inspect(docker: &std::ffi::OsStr) -> Vec<OsString> {
    [
        docker.to_owned(),
        "inspect".into(),
        "-f".into(),
        "{{.State.Running}}".into(),
        "pseudolife-mcp-daemon".into(),
    ]
    .into()
}
/// The daemon container's own Python CLI is the admitted deployment transport.
pub fn container_exec(
    docker: &std::ffi::OsStr,
    terminal: bool,
    mode: &str,
    arguments: &[OsString],
) -> Vec<OsString> {
    let mut command: Vec<_> = [
        docker.to_owned(),
        "exec".into(),
        if terminal { "-it".into() } else { "-i".into() },
        "-e".into(),
        "PSEUDOLIFE_DAEMON_EXEC=1".into(),
        "pseudolife-mcp-daemon".into(),
        "python".into(),
        "-m".into(),
        "pseudolife_memory.cli".into(),
        mode.into(),
    ]
    .into();
    command.extend_from_slice(arguments);
    command
}
