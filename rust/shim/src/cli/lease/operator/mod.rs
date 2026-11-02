//! Native offline operators. No agent HTTP or host Python storage bridge.
pub(crate) mod secrets;
pub mod store;

use super::{args::Args, out, say};
use crate::pg::{self, resolution};
use std::{
    ffi::OsString,
    time::{SystemTime, UNIX_EPOCH},
};

fn no_bank() -> i32 {
    say(
        "no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run where the lite tier's data dir holds one (Docker tier: start the daemon, or run `docker exec -it pseudolife-mcp-daemon pseudolife-mcp lease ...` on its host)",
    );
    1
}
fn diagnostic(error: &pg::Error) -> String {
    match error {
        // Unknown option text is supplied by the DSN and can be a malformed
        // password fragment. Never include it in a public operator diagnostic.
        pg::Error::UnsupportedOption(_) => "unsupported PostgreSQL DSN option".into(),
        error => error.to_string(),
    }
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
    let arguments: Vec<OsString> = arguments.iter().map(OsString::from).collect();
    resolution::run_in_daemon("lease", &arguments, None, None)
        .await
        .unwrap_or_else(no_bank)
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
            say(&format!("cannot open the bank ({})", diagnostic(&error)));
            return 1;
        }
    };
    let mut session = match pg::Session::open(&dsn).await {
        Ok(session) => session,
        Err(error) => {
            say(&format!("cannot open the bank ({})", diagnostic(&error)));
            return 1;
        }
    };
    let clock = || match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(time) => time.as_secs_f64(),
        Err(before) => -before.duration().as_secs_f64(),
    };
    let result = if args.action == "conductor" {
        store::grant_conductor(
            &mut session,
            args.name.as_deref().expect("conductor project"),
            args.agent.as_deref().expect("conductor agent"),
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
            say(&format!("{} refused: {code}", args.action));
            1
        }
        Err(store::Error::Database) => {
            say(&format!(
                "{} failed (PostgreSQL transaction failed); nothing was changed",
                args.action
            ));
            1
        }
        Err(store::Error::CommitUnknown) => {
            say(&format!(
                "{} lost the answer to its COMMIT (PostgreSQL session ended); it may have been committed: run `pseudolife-mcp lease list` before retrying",
                args.action
            ));
            1
        }
        Err(store::Error::Clock) => {
            say(&format!(
                "{} failed (system clock unavailable); nothing was changed",
                args.action
            ));
            1
        }
    }
}
