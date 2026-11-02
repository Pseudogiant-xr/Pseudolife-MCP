//! Container forwarding; all named files belong to the invoking host.
use super::{
    args::{Export, Head, Stats},
    bank, stats,
};
use crate::pg::resolution;
use std::{ffi::OsString, fs::File, path::PathBuf, process::Stdio};

fn runtime() -> Option<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .ok()
}
fn add(args: &mut Vec<OsString>, flag: &str, value: Option<impl ToString>) {
    if let Some(value) = value {
        args.extend([flag.into(), value.to_string().into()]);
    }
}
fn no_bank() -> u8 {
    stats::error(
        "no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run where the lite tier's data dir holds one (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp board-audit ...` on its host)",
    )
}
fn local_deferred() -> bool {
    if std::env::var_os("PSEUDOLIFE_MCP_DATABASE_URL").is_some_and(|v| !v.is_empty()) {
        return true;
    }
    let directory = std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let default = dirs::data_local_dir()
                .unwrap_or_default()
                .join("pseudolife-mcp");
            if default.exists() {
                default
            } else {
                std::env::current_dir().unwrap_or_default().join("data")
            }
        });
    directory.join("embedded_pg").join("PG_VERSION").exists()
}
fn forward(arguments: Vec<OsString>, stdout: Option<Stdio>) -> u8 {
    if local_deferred() {
        return super::deferred();
    }
    let Some(runtime) = runtime() else {
        return super::failed();
    };
    runtime
        .block_on(resolution::run_in_daemon(
            "board-audit",
            &arguments,
            None,
            stdout,
        ))
        .map_or_else(no_bank, |code| code as u8)
}
pub(super) fn verify(head: Option<&Head>) -> u8 {
    let mut args = vec!["verify".into()];
    add(
        &mut args,
        "--expect-head",
        head.map(|(seq, hash)| format!("{seq}:{hash}")),
    );
    forward(args, None)
}
pub(super) fn redact(message_id: &str, reason: &str) -> u8 {
    let mut args = vec!["redact".into()];
    add(&mut args, "--message-id", Some(message_id));
    add(&mut args, "--reason", Some(reason));
    forward(args, None)
}
pub(super) fn export(export: &Export) -> u8 {
    if local_deferred() {
        return super::deferred();
    }
    let mut args = vec!["export".into()];
    add(&mut args, "--project", export.project.as_deref());
    add(&mut args, "--task", export.task.as_deref());
    add(&mut args, "--agent", export.agent.as_deref());
    add(
        &mut args,
        "--since",
        export.since.map(crate::cli::pyjson::float_repr),
    );
    add(
        &mut args,
        "--until",
        export.until.map(crate::cli::pyjson::float_repr),
    );
    let Some(out) = &export.out else {
        return forward(args, None);
    };
    let file = match std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(out)
    {
        Ok(f) => f,
        Err(e) => {
            return stats::error(format!(
                "cannot create {out}: {}",
                bank::write_strerror(&e).unwrap_or_else(|| "I/O error".into())
            ));
        }
    };
    let code = match file.try_clone() {
        Ok(f) => forward(args, Some(Stdio::from(f))),
        Err(_) => super::failed(),
    };
    if code != 0 && bank::created_destination(&file, std::path::Path::new(out)) {
        let _ = std::fs::remove_file(out);
    }
    code
}
pub(super) fn stats(stats: &Stats) -> u8 {
    if local_deferred() {
        return super::deferred();
    }
    let Some(runtime) = runtime() else {
        return super::failed();
    };
    runtime.block_on(async {
        let Some((docker,rust)) = resolution::daemon().await else { return no_bank(); };
        let mut args = vec!["stats".into()];
        add(&mut args,"--since",stats.since.map(crate::cli::pyjson::float_repr));
        add(&mut args,"--until",stats.until.map(crate::cli::pyjson::float_repr));
        let durations = File::open(stats::durations_path(stats)).ok();
        if durations.is_some() { add(&mut args,"--durations",Some("/dev/stdin")); }
        let invocation = resolution::container_exec_for_engine(&docker,false,"board-audit",&args,rust);
        crate::stderrln!("pseudolife-mcp board-audit: no bank in this shell; running it inside the pseudolife-mcp-daemon container");
        let output = tokio::process::Command::new(&invocation[0]).args(&invocation[1..])
            .stdin(durations.map(Stdio::from).unwrap_or_else(Stdio::inherit))
            .stdout(Stdio::piped()).stderr(Stdio::inherit()).kill_on_drop(true).output().await;
        match output {
            Err(_) => no_bank(),
            Ok(output) if !output.status.success() => resolution::exit_code(output.status) as u8,
            Ok(output) => stats::emit(stats,String::from_utf8_lossy(&output.stdout).as_bytes()),
        }
    })
}
