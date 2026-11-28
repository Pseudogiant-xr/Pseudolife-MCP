//! Keep what a backup writes, and the password it dumps with, to the owner.
//!
//! On a shared Unix host another account can read a process's argv from
//! `/proc/<pid>/cmdline` and any file whose mode the umask left readable,
//! so there the password reaches `pg_dump` through its environment
//! (readable only by the same account), and the partial files and any
//! backup directory this run creates are owner-only. Windows shows a
//! process's command line only to its own account and administrators, so
//! the connection string still goes to `pg_dump` as written there: the
//! environment would re-encode a percent-encoded password through the ANSI
//! code page.
use std::{
    ffi::OsString,
    fs, io,
    path::{Path, PathBuf},
};

/// How `pg_dump` is pointed at the bank. Its `Debug` never shows the
/// password, so no panic or log line can carry it.
#[derive(PartialEq, Eq)]
pub(super) struct Connection {
    /// The `--dbname` argument, `None` when nothing but secrets was given
    /// (libpq then takes the database from its environment, as before).
    pub dbname: Option<String>,
    /// The `password` libpq would have used, for `PGPASSWORD`.
    pub password: Option<OsString>,
}

impl std::fmt::Debug for Connection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Connection")
            .field("has_dbname", &self.dbname.is_some())
            .field("has_password", &self.password.is_some())
            .finish()
    }
}

/// The `pg_dump` connection for `dsn`; `pgservice` says `PGSERVICE` is set.
/// An `Err` is the reason and its fix, ending in "then retry."
#[cfg(not(unix))]
pub(super) fn pg_dump_connection(dsn: &str, _pgservice: bool) -> Result<Connection, String> {
    Ok(Connection {
        dbname: Some(dsn.to_owned()),
        password: None,
    })
}

/// The `pg_dump` connection for `dsn`; `pgservice` says `PGSERVICE` is set.
/// An `Err` is the reason and its fix, ending in "then retry."
#[cfg(unix)]
pub(super) fn pg_dump_connection(dsn: &str, pgservice: bool) -> Result<Connection, String> {
    use std::os::unix::ffi::OsStringExt;
    let split = super::conninfo::split_secrets(dsn).ok_or_else(|| {
        "PSEUDOLIFE_MCP_DATABASE_URL is not a connection string libpq would accept (for example an unclosed quote, a word without '=', a malformed %-escape or an empty or doubled '=' in a URI option); fix it, then retry.".to_owned()
    })?;
    if let Some(key) = split.uncarried {
        return Err(format!(
            "PSEUDOLIFE_MCP_DATABASE_URL carries {key}, which pg_dump could only be given on its command line, where other accounts on this host can read it; move it and any password into an entry of your pg_service.conf, name that entry with service=<name> in the URL, then retry."
        ));
    }
    if split.password.is_some() && (split.service || pgservice) {
        return Err("PSEUDOLIFE_MCP_DATABASE_URL carries a password and also selects a service (service= or PGSERVICE); once the password leaves pg_dump's command line, a password in that service's entry would take its place; put the password in that service's entry instead, then retry.".to_owned());
    }
    Ok(Connection {
        dbname: split.dsn,
        password: split.password.map(OsString::from_vec),
    })
}

/// A partial-file path for `target` that no other run uses, so two backups
/// started in the same second never write into or publish each other's.
pub(super) fn partial_path(target: &Path) -> PathBuf {
    let mut name = target.as_os_str().to_owned();
    name.push(format!(
        ".{}.part",
        &uuid::Uuid::new_v4().simple().to_string()[..12]
    ));
    PathBuf::from(name)
}

/// Create a new file only its owner can read or write (owner-only on Unix;
/// Windows files inherit the folder's access list as before). An existing
/// file of that name is an error, never reused.
pub(super) fn create_file(path: &Path) -> io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

/// `create_dir_all`, with every directory it creates owner-only on Unix. An
/// existing directory keeps the mode its owner gave it.
pub(super) fn create_dir_all(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)
    }
    #[cfg(not(unix))]
    fs::create_dir_all(path)
}

#[cfg(test)]
mod tests {
    #[test]
    fn each_run_gets_its_own_partial_file_and_never_reuses_one() {
        let dir =
            std::env::temp_dir().join(format!("pl-private-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir(&dir).unwrap();
        let target = dir.join("x.sql.gz");
        let (first, second) = (super::partial_path(&target), super::partial_path(&target));
        let names =
            [&first, &second].map(|p| p.file_name().unwrap().to_string_lossy().into_owned());
        let created = super::create_file(&first).map(drop);
        let again = super::create_file(&first).map(drop);
        #[cfg(unix)]
        let mode = {
            use std::os::unix::fs::PermissionsExt;
            std::fs::metadata(&first).unwrap().permissions().mode() & 0o777
        };
        let nested = dir.join("a").join("b");
        super::create_dir_all(&nested).unwrap();
        #[cfg(unix)]
        let dir_modes: Vec<u32> = [dir.join("a"), nested.clone()]
            .iter()
            .map(|path| {
                use std::os::unix::fs::PermissionsExt;
                std::fs::metadata(path).unwrap().permissions().mode() & 0o777
            })
            .collect();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_ne!(first, second);
        for name in &names {
            assert!(
                name.starts_with("x.sql.gz.") && name.ends_with(".part"),
                "{name}"
            );
        }
        assert!(created.is_ok());
        assert_eq!(again.unwrap_err().kind(), std::io::ErrorKind::AlreadyExists);
        #[cfg(unix)]
        {
            assert_eq!(mode, 0o600);
            // The process umask can only narrow 0700 further.
            assert!(
                dir_modes.iter().all(|mode| mode & 0o077 == 0),
                "{dir_modes:?}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn decoded_password_bytes_reach_the_environment_unchanged() {
        use std::os::unix::ffi::OsStrExt;
        let connection =
            super::pg_dump_connection("postgresql://owner:%E9@db/bank", false).unwrap();
        assert_eq!(connection.password.unwrap().as_bytes(), b"\xe9");
    }

    #[cfg(unix)]
    #[test]
    fn secrets_without_an_environment_variable_are_refused_with_the_fix() {
        for dsn in [
            "postgresql://owner@db/bank?sslpassword=k",
            "host=db sslpassword='k ey'",
            "host=db oauth_client_secret=s",
        ] {
            let error = super::pg_dump_connection(dsn, false).unwrap_err();
            assert!(error.contains("service=<name>"));
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_password_beside_a_service_is_refused_with_the_fix() {
        for (dsn, pgservice) in [
            ("host=db service=mine password=pw", false),
            ("postgresql://owner:pw@db/bank", true),
        ] {
            let error = super::pg_dump_connection(dsn, pgservice).unwrap_err();
            assert!(error.contains("in that service's entry instead"));
        }
        // A service with no password in the string is left to libpq.
        assert_eq!(
            super::pg_dump_connection("host=db service=mine", true),
            Ok(super::Connection {
                dbname: Some("host=db service=mine".into()),
                password: None,
            })
        );
    }

    #[cfg(not(unix))]
    #[test]
    fn windows_passes_the_connection_string_as_written() {
        let dsn = "postgresql://owner:%C3%A9@db/bank?sslpassword=k";
        assert_eq!(
            super::pg_dump_connection(dsn, true),
            Ok(super::Connection {
                dbname: Some(dsn.into()),
                password: None,
            })
        );
    }
}
