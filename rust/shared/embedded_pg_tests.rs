use super::*;

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "pl-embedded-{label}-{}-{:x}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn env_of(pairs: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<OsString> {
    move |name| {
        pairs
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| OsString::from(value))
    }
}

#[test]
fn default_data_dir_follows_the_oracle_per_platform() {
    let home = Path::new("/home/u");
    let windows = default_data_dir(
        Platform::Windows,
        &env_of(&[("LOCALAPPDATA", "L")]),
        Some(home),
    );
    assert_eq!(windows, Some(PathBuf::from("L").join("pseudolife-mcp")));
    // An empty LOCALAPPDATA reads as unset (`os.environ.get(..) or`).
    let windows = default_data_dir(
        Platform::Windows,
        &env_of(&[("LOCALAPPDATA", "")]),
        Some(home),
    );
    assert_eq!(
        windows,
        Some(home.join("AppData").join("Local").join("pseudolife-mcp"))
    );
    let mac = default_data_dir(
        Platform::MacOs,
        &env_of(&[("XDG_DATA_HOME", "X")]),
        Some(home),
    );
    assert_eq!(
        mac,
        Some(
            home.join("Library")
                .join("Application Support")
                .join("pseudolife-mcp")
        )
    );
    let linux = default_data_dir(
        Platform::Other,
        &env_of(&[("XDG_DATA_HOME", "X")]),
        Some(home),
    );
    assert_eq!(linux, Some(PathBuf::from("X").join("pseudolife-mcp")));
    let linux = default_data_dir(Platform::Other, &env_of(&[]), Some(home));
    assert_eq!(
        linux,
        Some(home.join(".local").join("share").join("pseudolife-mcp"))
    );
    assert_eq!(default_data_dir(Platform::Other, &env_of(&[]), None), None);
    assert_eq!(pgdata(Path::new("d")), Path::new("d").join("embedded_pg"));
}

#[test]
fn non_ascii_pgdata_is_refused_on_windows_only() {
    let path = Path::new("C:\\data\\Zoë\\pseudolife-mcp\\embedded_pg");
    let error = preflight_path(path, Platform::Windows).unwrap_err();
    assert!(matches!(error, Error::NonAsciiPath(_)));
    assert_eq!(
        error.to_string(),
        "embedded postgres data dir 'C:\\\\data\\\\Zoë\\\\pseudolife-mcp\\\\embedded_pg' contains \
         non-ASCII characters, which the embedded PostgreSQL runtime cannot handle on Windows. \
         Set PSEUDOLIFE_MCP_DATA_DIR to an ASCII-only path (for example C:\\pseudolife-data) \
         and restart the daemon."
    );
    // Spaces are fine (verified live for the oracle), and other platforms pass.
    preflight_path(
        Path::new("C:\\Program Data\\pl\\embedded_pg"),
        Platform::Windows,
    )
    .unwrap();
    preflight_path(path, Platform::Other).unwrap();
    preflight_path(path, Platform::MacOs).unwrap();
}

#[test]
fn pg_version_major_guard_matches_the_oracle() {
    let dir = scratch("major");
    // No PG_VERSION yet: a fresh pgdata passes.
    guard_pg_major(&dir).unwrap();
    for ok in ["18", "18\n", " 18.1 \r\n"] {
        fs::write(dir.join("PG_VERSION"), ok).unwrap();
        guard_pg_major(&dir).unwrap();
    }
    fs::write(dir.join("PG_VERSION"), "16\n").unwrap();
    let error = guard_pg_major(&dir).unwrap_err();
    assert!(matches!(&error, Error::MajorMismatch { found, .. } if found == "16"));
    assert!(
        error
            .to_string()
            .contains("was initialized by PostgreSQL 16, but")
    );
    assert!(
        error
            .to_string()
            .contains("must never be re-initialized in place")
    );
    fs::write(dir.join("PG_VERSION"), b"x\xff\n").unwrap();
    let error = guard_pg_major(&dir).unwrap_err();
    assert!(matches!(&error, Error::UnreadablePgVersion { found, .. } if found == "x\u{fffd}"));
    assert!(
        error
            .to_string()
            .ends_with("has an unreadable PG_VERSION ('x\u{fffd}'); refusing to touch it.")
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn py_repr_quotes_as_python_does() {
    assert_eq!(py_repr("a"), "'a'");
    assert_eq!(py_repr("a\\b"), "'a\\\\b'");
    assert_eq!(py_repr("it's"), "\"it's\"");
    assert_eq!(py_repr("it's \"x\""), "'it\\'s \"x\"'");
}

#[test]
fn postmaster_pid_yields_pid_and_port() {
    let text = "4242\nC:/data/embedded_pg\n1792890000\n5433\n\nlocalhost\n  1234567\nready   \n";
    assert_eq!(
        PidFile::parse(text),
        Some(PidFile {
            pid: 4242,
            port: 5433
        })
    );
    // CRLF, as a file edited on Windows may carry.
    assert_eq!(
        PidFile::parse("7\r\nd\r\n1\r\n6000\r\n"),
        Some(PidFile { pid: 7, port: 6000 })
    );
    assert_eq!(PidFile::parse("4242\n"), None);
    assert_eq!(PidFile::parse("x\nd\n1\n5433\n"), None);
}

#[test]
fn only_postgres_images_count_as_a_running_server() {
    // Each OS reports images in its own path form.
    if cfg!(windows) {
        assert!(is_postgres_image("C:\\pg\\bin\\postgres.exe"));
        assert!(!is_postgres_image("C:\\pg\\bin\\pg_ctl.exe"));
    } else {
        assert!(is_postgres_image("/usr/lib/postgresql/18/bin/postgres\n"));
        assert!(!is_postgres_image("/usr/lib/postgresql/18/bin/pg_ctl"));
    }
    assert!(is_postgres_image("postgres"));
    assert!(!is_postgres_image(""));
    // pid 0, and this test process (alive, but no postgres).
    assert!(!is_postgres_process(0));
    assert!(!is_postgres_process(std::process::id()));
}

#[test]
fn an_incomplete_runtime_is_refused_naming_what_is_missing() {
    let dir = scratch("runtime");
    let error = Runtime::new(dir.clone()).unwrap_err();
    let exe = if cfg!(windows) { ".exe" } else { "" };
    assert!(
        matches!(&error, Error::Runtime { missing, .. } if *missing == format!("bin/initdb{exe}"))
    );
    fs::create_dir_all(dir.join("bin")).unwrap();
    for program in ["initdb", "pg_ctl", "postgres", "psql"] {
        fs::write(dir.join("bin").join(format!("{program}{exe}")), b"").unwrap();
    }
    let error = Runtime::new(dir.clone()).unwrap_err();
    assert!(matches!(&error, Error::Runtime { missing, .. } if missing.contains("vector.control")));
    fs::create_dir_all(dir.join("share").join("extension")).unwrap();
    fs::write(
        dir.join("share").join("extension").join("vector.control"),
        b"",
    )
    .unwrap();
    assert_eq!(Runtime::new(dir.clone()).unwrap().root(), dir);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn the_start_lock_sits_beside_the_pgdata() {
    let pgdata = Path::new("d").join("embedded_pg");
    assert_eq!(
        StartLock::path(&pgdata),
        Path::new("d").join("embedded_pg.lock")
    );
}

/// A plain-file `SecretStore` for these cases (the shim's owner-only store
/// has its own tests in `embedded_secret_tests.rs`).
pub(crate) struct PlainSecrets;
impl SecretStore for PlainSecrets {
    fn read(&self, path: &Path) -> Result<Option<String>, String> {
        match fs::read_to_string(path) {
            Ok(text) => Ok(Some(text.trim().to_owned())),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.to_string()),
        }
    }
    fn create(&self, path: &Path) -> Result<String, String> {
        let secret = format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        fs::write(path, &secret).map_err(|e| e.to_string())?;
        Ok(secret)
    }
}

struct FixedSecret(&'static str);
impl SecretStore for FixedSecret {
    fn read(&self, _: &Path) -> Result<Option<String>, String> {
        Ok(Some(self.0.to_owned()))
    }
    fn create(&self, _: &Path) -> Result<String, String> {
        unreachable!()
    }
}

#[test]
fn the_secret_sits_beside_the_pgdata_and_must_be_64_hex() {
    let pgdata = Path::new("d").join("embedded_pg");
    assert_eq!(
        secret_path(&pgdata),
        Path::new("d").join("embedded_pg.secret")
    );
    let good = "0123456789abcdef".repeat(4);
    assert_eq!(
        cluster_secret(
            &FixedSecret(Box::leak(good.clone().into_boxed_str())),
            &pgdata
        )
        .unwrap(),
        (good, false)
    );
    for bad in ["postgres", "ABCDEF", "' OR 1=1 --"] {
        let error = cluster_secret(&FixedSecret(bad), &pgdata).unwrap_err();
        assert!(matches!(error, Error::Secret { .. }));
        assert!(error.to_string().contains("Move it aside and start again"));
    }
    let dir = scratch("secret");
    let pgdata = dir.join("embedded_pg");
    let (created, fresh) = cluster_secret(&PlainSecrets, &pgdata).unwrap();
    assert!(fresh);
    assert_eq!(
        cluster_secret(&PlainSecrets, &pgdata).unwrap(),
        (created, false)
    );
    fs::remove_dir_all(dir).unwrap();
}

const LEGACY_HBA: &str = "\
# TYPE  DATABASE        USER            ADDRESS                 METHOD\n\
local   all             all                                     password\n\
host    all             all             127.0.0.1/32            password\n\
host    all             all             ::1/128                 password\n\
host    replication     all             127.0.0.1/32            password\n";

#[test]
fn hba_rewrite_changes_only_password_style_methods() {
    let text = "# host all all 0.0.0.0/0 trust   (a comment, untouched)\n\
                \n\
                local   all   all                       password\n\
                host    all   all   127.0.0.1/32        md5\n\
                host    all   all   10.0.0.0 255.0.0.0  scram-sha-256\r\n\
                host    all   all   0.0.0.0/0           reject\n\
                include_dir conf.d\n\
                hostssl all   all   ::1/128             cert clientcert=verify-full\n";
    let out = rewrite_hba(text, "trust", false);
    let lines: Vec<&str> = out.split('\n').collect();
    assert_eq!(
        lines[0],
        "# host all all 0.0.0.0/0 trust   (a comment, untouched)"
    );
    assert_eq!(lines[2], "local   all   all                       trust");
    assert_eq!(lines[3], "host    all   all   127.0.0.1/32        trust");
    // CRLF line endings survive.
    assert_eq!(lines[4], "host    all   all   10.0.0.0 255.0.0.0  trust\r");
    // Unmanaged rules and include directives stay byte for byte.
    let original: Vec<&str> = text.split('\n').collect();
    assert_eq!(lines[5..8], original[5..8]);
    assert_eq!(managed_methods(&out), BTreeSet::from(["trust".to_owned()]));
    // Unmanaged rules do not make a SCRAM bank look unmigrated.
    assert!(scram_only(&managed_methods(&rewrite_hba(
        text,
        "scram-sha-256",
        false
    ))));
    assert!(managed_methods("# only comments\n").is_empty());
    assert_eq!(
        managed_methods(LEGACY_HBA),
        BTreeSet::from(["password".to_owned()])
    );
    let scram = rewrite_hba(LEGACY_HBA, "scram-sha-256", false);
    assert!(scram_only(&managed_methods(&scram)));
    assert!(scram.contains("127.0.0.1/32            scram-sha-256\n"));
    assert_eq!(scram.lines().next(), LEGACY_HBA.lines().next());
}

#[test]
fn the_scram_verifier_passes_rfc_7677_s_published_exchange() {
    // RFC 7677 section 3: password "pencil", this salt, 4096 iterations.
    use hmac::{Hmac, Mac};
    use sha2::{Digest, Sha256};
    let decode = |text: &str| -> Vec<u8> {
        let table = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut bits = 0u32;
        let mut count = 0;
        let mut out = Vec::new();
        for c in text.bytes().filter(|&c| c != b'=') {
            bits = bits << 6 | table.iter().position(|&t| t == c).unwrap() as u32;
            count += 6;
            if count >= 8 {
                count -= 8;
                out.push((bits >> count) as u8);
            }
        }
        out
    };
    let salt: [u8; 16] = decode("W22ZaJ0SNY7soEsUEjb6gQ==").try_into().unwrap();
    let verifier = scram_verifier("pencil", salt, 4096);
    assert!(verifier.starts_with("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$"));
    let keys: Vec<Vec<u8>> = verifier
        .rsplit('$')
        .next()
        .unwrap()
        .split(':')
        .map(decode)
        .collect();
    let nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0";
    let auth = format!(
        "n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r={nonce}"
    );
    let mac = |key: &[u8]| {
        let mut mac = Hmac::<Sha256>::new_from_slice(key).unwrap();
        mac.update(auth.as_bytes());
        mac.finalize().into_bytes().to_vec()
    };
    let proof = decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=");
    let client_key: Vec<u8> = proof
        .iter()
        .zip(mac(&keys[0]))
        .map(|(a, b)| a ^ b)
        .collect();
    assert_eq!(Sha256::digest(&client_key).to_vec(), keys[0]);
    assert_eq!(
        mac(&keys[1]),
        decode("6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")
    );
    assert_eq!(base64(b"ab"), "YWI=");
    assert_eq!(base64(b"abc"), "YWJj");
    assert_eq!(base64(b"a"), "YQ==");
}

#[test]
fn a_handle_redacts_its_password_and_hands_out_a_scram_only_dsn() {
    let secret = "5e".repeat(32);
    let handle = Embedded {
        pgdata: PathBuf::from("d"),
        port: 5999,
        password: secret.clone(),
        owned: None,
    };
    assert!(!format!("{handle:?}").contains(&secret));
    assert!(
        handle
            .dsn()
            .ends_with("/pseudolife_memory?require_auth=scram-sha-256")
    );
}

#[test]
fn stop_leaves_a_server_another_caller_restarted() {
    // The launched server exited and another caller started the bank again:
    // postmaster.pid names another pid, so stop must not run pg_ctl (the
    // runtime here has no binaries; running one would fail).
    let dir = scratch("stopid");
    let pgdata = dir.join("embedded_pg");
    fs::create_dir_all(&pgdata).unwrap();
    fs::write(pgdata.join("postmaster.pid"), "2222\nd\n1\n5999\n").unwrap();
    let handle = Embedded {
        pgdata: pgdata.clone(),
        port: 5999,
        password: "5e".repeat(32),
        owned: Some((
            Runtime {
                root: dir.join("no-runtime"),
            },
            Some(1111),
        )),
    };
    handle.stop().unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn the_trust_marker_touches_only_local_and_loopback_rules() {
    let remote = "host    all             all             10.0.0.0/8              password\n";
    let text = format!("{LEGACY_HBA}{remote}");
    let marked = rewrite_hba(&text, "trust", true);
    assert!(marked.contains(remote));
    assert!(is_marked(&marked));
    assert!(!is_marked(&text));
    assert!(!is_marked(&rewrite_hba(&text, "scram-sha-256", false)));
    let loopback = |rule: &str| is_loopback_rule(&rule.split_whitespace().collect::<Vec<_>>());
    assert!(loopback("local all all password"));
    assert!(loopback("host all all 127.0.0.1/32 md5"));
    assert!(loopback("host all all ::1/128 md5"));
    assert!(loopback("host all all 127.0.0.1 255.255.255.255 md5"));
    assert!(loopback("host all all localhost md5"));
    assert!(!loopback("host all all 127.0.0.0/8 md5"));
    assert!(!loopback("host all all samehost md5"));
    assert!(!loopback("host all all 10.0.0.0/8 md5"));
}

/// Cases that run a real PostgreSQL 18 with pgvector: set
/// `PSEUDOLIFE_TEST_PG_RUNTIME` to an installation (`bin/`, `share/`), such as
/// a pg0 `installation/18.1.0`. Without it they report and return.
pub(crate) fn real_runtime() -> Option<Runtime> {
    let Some(root) = std::env::var_os("PSEUDOLIFE_TEST_PG_RUNTIME").filter(|v| !v.is_empty())
    else {
        eprintln!("PSEUDOLIFE_TEST_PG_RUNTIME unset: real-runtime case not run");
        return None;
    };
    Some(Runtime::new(PathBuf::from(root)).expect("PSEUDOLIFE_TEST_PG_RUNTIME is a runtime"))
}

#[test]
fn a_fresh_bank_starts_owned_then_a_second_caller_attaches() {
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("fresh");
    let first = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(first.owned());
    assert_eq!(
        fs::read_to_string(dir.join("embedded_pg").join("PG_VERSION"))
            .unwrap()
            .trim(),
        "18"
    );
    // The bank has its own secret and SCRAM; the old fixed password fails.
    let secret = fs::read_to_string(dir.join("embedded_pg.secret")).unwrap();
    assert_eq!(first.password(), secret);
    assert!(first.dsn().contains(&format!(":{secret}@")));
    let hba = fs::read_to_string(dir.join("embedded_pg").join("pg_hba.conf")).unwrap();
    assert!(scram_only(&managed_methods(&hba)));
    assert!(psql(&runtime, first.port, LEGACY_PASSWORD, "SELECT 1").is_err());
    // The bank's database exists and pgvector loads in it.
    let vector = psql_in(
        &runtime,
        &first,
        "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname = 'vector'",
    );
    assert_eq!(vector, "0.8.5");
    assert_eq!(psql_in(&runtime, &first, "SHOW timezone"), "UTC");
    // fsync stays on (decision 7), unlike pg0's -F.
    assert_eq!(psql_in(&runtime, &first, "SHOW fsync"), "on");
    let second = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(!second.owned());
    assert_eq!(second.port, first.port);
    assert_eq!(second.dsn(), first.dsn());
    // Stopping an attached handle leaves the server running.
    second.stop().unwrap();
    assert!(dir.join("embedded_pg").join("postmaster.pid").exists());
    first.stop().unwrap();
    assert!(!dir.join("embedded_pg").join("postmaster.pid").exists());
    // A restart reuses the cluster, its database and its secret.
    let again = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(again.owned());
    assert_eq!(again.password(), secret);
    assert_eq!(
        psql_in(
            &runtime,
            &again,
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        ),
        "1"
    );
    again.stop().unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_legacy_bank_moves_to_its_secret_on_its_next_owned_start() {
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("legacy");
    let pgdata = dir.join("embedded_pg");
    fs::create_dir_all(&pgdata).unwrap();
    // What pg0 made before 2026-10-25: the fixed password, cleartext auth.
    let pwfile = dir.join("legacy.pw");
    fs::write(&pwfile, LEGACY_PASSWORD).unwrap();
    let mut initdb = runtime.command("initdb");
    initdb
        .arg("-D")
        .arg(&pgdata)
        .args([
            "-U",
            SUPERUSER,
            "--auth=password",
            "--encoding=UTF8",
            "--locale=C",
        ])
        .arg(format!("--pwfile={}", pwfile.display()));
    run_logged(initdb, "initdb", &pgdata).unwrap();
    let moved = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(moved.owned());
    assert_ne!(moved.password(), LEGACY_PASSWORD);
    assert!(psql(&runtime, moved.port, LEGACY_PASSWORD, "SELECT 1").is_err());
    assert_eq!(psql_in(&runtime, &moved, "SELECT 1"), "1");
    let hba = fs::read_to_string(pgdata.join("pg_hba.conf")).unwrap();
    assert!(scram_only(&managed_methods(&hba)));
    moved.stop().unwrap();
    // An interrupted move (the trust marker left behind) finishes too.
    fs::write(pgdata.join("pg_hba.conf"), rewrite_hba(&hba, "trust", true)).unwrap();
    let finished = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(psql(&runtime, finished.port, LEGACY_PASSWORD, "SELECT 1").is_err());
    let hba = fs::read_to_string(pgdata.join("pg_hba.conf")).unwrap();
    assert!(scram_only(&managed_methods(&hba)));
    finished.stop().unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_stale_pid_file_is_cleared_and_the_bank_starts() {
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("stale");
    attach_or_start(&dir, &runtime, &PlainSecrets)
        .unwrap()
        .stop()
        .unwrap();
    // A record naming a live process that is no postgres (this test), as
    // after a reboot that reassigned the pid.
    let pid_file = dir.join("embedded_pg").join("postmaster.pid");
    fs::write(&pid_file, format!("{}\nx\n1\n5999\n", std::process::id())).unwrap();
    let embedded = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(embedded.owned());
    assert_ne!(embedded.port, 5999);
    embedded.stop().unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn concurrent_first_boots_start_one_server() {
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("race");
    let callers: Vec<_> = (0..2)
        .map(|_| {
            let (dir, runtime) = (dir.clone(), runtime.clone());
            std::thread::spawn(move || attach_or_start(&dir, &runtime, &PlainSecrets).unwrap())
        })
        .collect();
    let mut results: Vec<Embedded> = callers.into_iter().map(|c| c.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|e| e.owned()).count(), 1);
    assert_eq!(results[0].port, results[1].port);
    assert_eq!(results[0].password(), results[1].password());
    results.sort_by_key(|e| e.owned());
    for embedded in results {
        embedded.stop().unwrap();
    }
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_stopped_bank_on_scram_rules_with_the_old_role_password_finishes_its_move() {
    // A move interrupted between its SCRAM rewrite and its role change,
    // then stopped: the rules say SCRAM, the role still takes the public
    // password. The owned start asks the server and moves the role.
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("scramold");
    let pgdata = dir.join("embedded_pg");
    fs::create_dir_all(&pgdata).unwrap();
    let pwfile = dir.join("legacy.pw");
    fs::write(&pwfile, LEGACY_PASSWORD).unwrap();
    let mut initdb = runtime.command("initdb");
    initdb
        .arg("-D")
        .arg(&pgdata)
        .args(["-U", SUPERUSER, "--auth=scram-sha-256", "--encoding=UTF8"])
        .arg(format!("--pwfile={}", pwfile.display()));
    run_logged(initdb, "initdb", &pgdata).unwrap();
    PlainSecrets.create(&secret_path(&pgdata)).unwrap();
    let moved = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap();
    assert!(moved.owned());
    assert!(psql(&runtime, moved.port, LEGACY_PASSWORD, "SELECT 1").is_err());
    assert_eq!(psql_in(&runtime, &moved, "SELECT 1"), "1");
    moved.stop().unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_running_postgres_serving_another_pgdata_is_refused_with_its_remedy() {
    // A reused pid (or a copied pgdata) can point postmaster.pid at a live
    // postgres that serves another bank: refuse, never take it for this one.
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("otherbank");
    let other = attach_or_start(&dir.join("a"), &runtime, &PlainSecrets).unwrap();
    let pgdata = dir.join("b").join("embedded_pg");
    fs::create_dir_all(&pgdata).unwrap();
    let record =
        fs::read_to_string(dir.join("a").join("embedded_pg").join("postmaster.pid")).unwrap();
    fs::write(pgdata.join("postmaster.pid"), record).unwrap();
    let error = attach_or_start(&dir.join("b"), &runtime, &PlainSecrets).unwrap_err();
    let text = error.to_string();
    assert!(text.contains("answers to neither"), "{text}");
    assert!(text.contains("may be busy"), "{text}");
    assert!(text.contains("postmaster.pid"), "{text}");
    // The other bank's server is untouched.
    assert_eq!(psql_in(&runtime, &other, "SELECT 1"), "1");
    other.stop().unwrap();
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_cross_major_pgdata_is_refused_before_anything_starts() {
    let Some(runtime) = real_runtime() else {
        return;
    };
    let dir = scratch("cross");
    fs::create_dir_all(dir.join("embedded_pg")).unwrap();
    fs::write(dir.join("embedded_pg").join("PG_VERSION"), "16\n").unwrap();
    let error = attach_or_start(&dir, &runtime, &PlainSecrets).unwrap_err();
    assert!(matches!(error, Error::MajorMismatch { .. }));
    assert!(!StartLock::path(&dir.join("embedded_pg")).exists());
    assert!(!secret_path(&dir.join("embedded_pg")).exists());
    fs::remove_dir_all(dir).unwrap();
}

/// psql against the bank's own database with the handle's password; the
/// last line of output.
pub(crate) fn psql_in(runtime: &Runtime, embedded: &Embedded, sql: &str) -> String {
    let output = runtime
        .command("psql")
        .args(["-X", "-w", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1"])
        .args([
            "-h",
            "127.0.0.1",
            "-p",
            &embedded.port.to_string(),
            "-U",
            SUPERUSER,
            "-d",
            DATABASE,
        ])
        .args(["-c", sql])
        .env("PGPASSWORD", embedded.password())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .last()
        .unwrap_or("")
        .trim()
        .to_owned()
}
