//! Native offline operators. No agent HTTP or host Python storage bridge.
mod secrets;
pub mod store;

use super::{args::Args, out, say};
use crate::pg::{self, resolution};
use std::{
    ffi::OsString,
    io::{IsTerminal, Write},
    process::Stdio,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::process::Command;

fn no_bank() -> i32 {
    say(
        "lease: no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run where the lite tier's data dir holds one (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp lease ...` on its host)",
    );
    1
}
fn configure(command: &mut Command) {
    command.kill_on_drop(true);
}
fn lite_directory() -> Result<std::path::PathBuf, pg::Error> {
    #[cfg(windows)]
    let home = std::env::var_os("USERPROFILE").or_else(|| {
        std::env::var_os("HOMEPATH").map(|path| {
            let mut home = std::env::var_os("HOMEDRIVE").unwrap_or_default();
            home.push(path);
            home
        })
    });
    #[cfg(not(windows))]
    let home = std::env::var_os("HOME");
    let home = home
        .map(std::path::PathBuf::from)
        .or_else(dirs::home_dir)
        .ok_or(pg::Error::Connection)?;
    #[cfg(windows)]
    let base = std::env::var_os("LOCALAPPDATA")
        .filter(|path| !path.is_empty())
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| home.join("AppData").join("Local"));
    #[cfg(target_os = "macos")]
    let base = home.join("Library").join("Application Support");
    #[cfg(all(not(windows), not(target_os = "macos")))]
    let base = std::env::var_os("XDG_DATA_HOME")
        .filter(|path| !path.is_empty())
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| home.join(".local").join("share"));
    Ok(base.join("pseudolife-mcp"))
}
async fn container(arguments: &[String]) -> i32 {
    if std::env::var_os("PSEUDOLIFE_DAEMON_EXEC").is_some_and(|v| !v.is_empty()) {
        return no_bank();
    }
    let docker = std::env::var_os("PSEUDOLIFE_DOCKER")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "docker".into());
    let inspect = resolution::container_inspect(&docker);
    let mut command = Command::new(&inspect[0]);
    command
        .args(&inspect[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configure(&mut command);
    let Ok(child) = command.spawn() else {
        return no_bank();
    };
    let Ok(Ok(output)) =
        tokio::time::timeout(Duration::from_secs(30), child.wait_with_output()).await
    else {
        return no_bank();
    };
    if !output.status.success()
        || String::from_utf8_lossy(&output.stdout).trim_matches(super::whitespace) != "true"
    {
        return no_bank();
    }
    let terminal = std::io::stdin().is_terminal() && std::io::stdout().is_terminal();
    let arguments: Vec<OsString> = arguments.iter().map(OsString::from).collect();
    let invocation = resolution::container_exec(&docker, terminal, "lease", &arguments);
    say(
        "pseudolife-mcp lease: no bank in this shell; running it inside the pseudolife-mcp-daemon container",
    );
    let _ = std::io::stderr().flush();
    let _ = std::io::stdout().flush();
    let mut command = Command::new(&invocation[0]);
    command.args(&invocation[1..]);
    configure(&mut command);
    match command.status().await {
        Ok(status) => {
            #[cfg(unix)]
            {
                use std::os::unix::process::ExitStatusExt;
                // Python forwards subprocess.returncode through SystemExit.
                status
                    .code()
                    .unwrap_or_else(|| (256 - status.signal().unwrap_or(1)) & 255)
            }
            #[cfg(windows)]
            {
                status.code().unwrap_or(1)
            }
        }
        Err(_) => no_bank(),
    }
}
fn resolution() -> Result<resolution::Target, pg::Error> {
    let dsn = match std::env::var("PSEUDOLIFE_MCP_DATABASE_URL") {
        Ok(dsn) => Some(dsn),
        Err(std::env::VarError::NotPresent) => None,
        Err(_) => return Err(pg::Error::InvalidDsn),
    };
    // An explicit DSN wins before any filesystem probing or embedded lifecycle.
    if let Some(dsn) = dsn.as_deref().filter(|v| !v.is_empty()) {
        return pg::Dsn::parse(dsn)
            .map(Box::new)
            .map(resolution::Target::Direct);
    }
    let explicit = std::env::var_os("PSEUDOLIFE_MCP_DATA_DIR").map(std::path::PathBuf::from);
    if let Some(data) = explicit
        .as_deref()
        .filter(|path| !path.as_os_str().is_empty())
    {
        return resolution::select(None, data, true);
    }
    // Embedded availability is deliberately not emulated by importing pg0 in
    // host Python. An existing lite bank gets the named Phase 4 refusal below.
    let lite = lite_directory()?;
    if lite.exists() {
        return resolution::select(None, &lite, true);
    }
    let cwd = std::env::current_dir().map_err(|_| pg::Error::Connection)?;
    resolution::select(None, &cwd.join("data"), true)
}
pub(super) async fn command(args: &Args, arguments: &[String]) -> i32 {
    let dsn = match resolution() {
        Ok(resolution::Target::Direct(dsn)) => dsn,
        Ok(resolution::Target::ExistingEmbedded(_)) => {
            say(&format!(
                "lease: {} refused: phase4-embedded-pg-deferred",
                args.action
            ));
            return 1;
        }
        Ok(resolution::Target::Container) => return container(arguments).await,
        Err(error) => {
            say(&format!("lease: cannot open the bank ({error})"));
            return 1;
        }
    };
    let mut session = match pg::Session::open(&dsn).await {
        Ok(session) => session,
        Err(error) => {
            say(&format!("lease: cannot open the bank ({error})"));
            return 1;
        }
    };
    let clock = || match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(time) => time.as_secs_f64(),
        Err(before) => -before.duration().as_secs_f64(),
    };
    let result = if args.action == "delegate" {
        store::grant_delegate(
            &mut session,
            args.name.as_deref().expect("delegate project"),
            args.agent.as_deref().expect("delegate agent"),
            args.hold,
            clock,
        )
        .await
        .and_then(|grant| Ok((grant.json_line()?, grant.warning())))
    } else {
        store::break_lease(
            &mut session,
            args.name.as_deref().expect("break name"),
            clock,
        )
        .await
        .map(|broken| (broken.json_line(), None))
    };
    // The dedicated connection is ours; the bank/embedded server is not.
    // Python's close is best effort after the mutation's observed commit.
    let _ = session.close().await;
    match result {
        Ok((json, warning)) => {
            out(&json, false);
            if let Some(warning) = warning {
                say(&warning);
            }
            0
        }
        Err(store::Error::Refused(code)) => {
            say(&format!("lease: {} refused: {code}", args.action));
            1
        }
        Err(store::Error::Database) => {
            say(&format!(
                "lease: {} failed (PostgreSQL transaction failed); nothing was changed",
                args.action
            ));
            1
        }
        Err(store::Error::Clock) => {
            say(&format!(
                "lease: {} failed (system clock unavailable); nothing was changed",
                args.action
            ));
            1
        }
    }
}
