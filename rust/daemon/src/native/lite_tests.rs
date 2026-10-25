use super::*;
use std::fs;

fn env_of(pairs: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<String> {
    move |name| {
        pairs
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| (*value).to_owned())
    }
}

#[test]
fn a_nonempty_dsn_wins_and_is_trusted_as_given() {
    let resolved = resolve(
        &env_of(&[
            ("PSEUDOLIFE_MCP_DATABASE_URL", "postgresql://u@h/db"),
            ("PSEUDOLIFE_MCP_STORAGE", "files"),
        ]),
        None,
    );
    assert_eq!(
        resolved,
        Ok(Storage::Explicit("postgresql://u@h/db".into()))
    );
}

#[test]
fn no_dsn_resolves_to_the_lite_data_dir() {
    let resolved = resolve(
        &env_of(&[
            ("PSEUDOLIFE_MCP_DATABASE_URL", ""),
            ("PSEUDOLIFE_MCP_DATA_DIR", "/srv/bank"),
            ("PSEUDOLIFE_MCP_STORAGE", " AUTO "),
        ]),
        None,
    );
    assert_eq!(resolved, Ok(Storage::Lite(PathBuf::from("/srv/bank"))));
    // With no data dir set, the per-user default (as the oracle's
    // default_lite_data_dir picks it).
    let home = Path::new("/home/u");
    let resolved = resolve(&env_of(&[]), Some(home)).unwrap();
    let expected = native::default_data_dir(Platform::current(), &|_| None, Some(home)).unwrap();
    assert_eq!(resolved, Storage::Lite(expected));
}

#[test]
fn file_mode_is_refused_naming_what_replaces_it() {
    for mode in ["files", " Files ", "FILES"] {
        let pairs: &'static [(&'static str, &'static str)] =
            Box::leak(vec![("PSEUDOLIFE_MCP_STORAGE", mode)].into_boxed_slice());
        let error = resolve(&env_of(pairs), Some(Path::new("/home/u"))).unwrap_err();
        assert!(
            error.contains("PSEUDOLIFE_MCP_STORAGE=files selects file mode"),
            "{error}"
        );
        assert!(error.contains("Unset PSEUDOLIFE_MCP_STORAGE"), "{error}");
        assert!(error.contains("PSEUDOLIFE_MCP_DATABASE_URL"), "{error}");
    }
}

#[test]
fn an_unrecognized_mode_is_refused_with_python_s_repr() {
    let error = resolve(
        &env_of(&[("PSEUDOLIFE_MCP_STORAGE", "Embedded")]),
        Some(Path::new("/home/u")),
    )
    .unwrap_err();
    assert!(
        error.starts_with("PSEUDOLIFE_MCP_STORAGE='embedded' is not recognized"),
        "{error}"
    );
    // Whitespace only strips to '' (the oracle reads the raw value, so it
    // is not the unset default).
    let error = resolve(
        &env_of(&[("PSEUDOLIFE_MCP_STORAGE", "   ")]),
        Some(Path::new("/home/u")),
    )
    .unwrap_err();
    assert!(
        error.starts_with("PSEUDOLIFE_MCP_STORAGE='' is not recognized"),
        "{error}"
    );
}

#[test]
fn no_home_and_no_data_dir_is_refused_with_its_fix() {
    let error = resolve(&env_of(&[]), None).unwrap_err();
    assert!(error.contains("set PSEUDOLIFE_MCP_DATA_DIR"), "{error}");
}

#[test]
fn connections_elsewhere_are_not_probed() {
    // No lite bank registered: nothing to probe.
    let config: tokio_postgres::Config = "postgresql://u@127.0.0.1:5432/db".parse().unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    assert_eq!(
        runtime.block_on(verify_before_connect(
            &config,
            std::time::Duration::from_secs(2)
        )),
        Ok(())
    );
}

/// Decision 6 against a real server (`PSEUDOLIFE_TEST_PG_RUNTIME` names a
/// provisioned runtime).
#[test]
fn legacy_state_is_refused_only_where_python_would_import_it() {
    let Some(root) = std::env::var_os("PSEUDOLIFE_TEST_PG_RUNTIME") else {
        eprintln!("skipped: set PSEUDOLIFE_TEST_PG_RUNTIME to a provisioned runtime");
        return;
    };
    let runtime = Runtime::new(PathBuf::from(root)).unwrap();
    let mut tag = [0u8; 8];
    getrandom::fill(&mut tag).unwrap();
    let tag: String = tag.iter().map(|b| format!("{b:02x}")).collect();
    let data_dir = std::env::temp_dir().join(format!("pl-lite-legacy-{tag}"));
    fs::create_dir_all(data_dir.join("memory_state")).unwrap();
    let embedded = embedded::attach_or_start(&data_dir, &runtime, &DaemonSecrets).unwrap();
    let lite = Lite {
        embedded,
        runtime,
        data_dir: data_dir.clone(),
    };
    // No legacy files: served.
    assert_eq!(legacy_state(&lite), Ok(()));
    fs::write(data_dir.join("cortex_state.pt"), b"legacy").unwrap();
    // A fresh bank (no tables yet) with legacy files: refused.
    let error = legacy_state(&lite).unwrap_err();
    assert!(error.contains("cortex_state.pt"), "{error}");
    assert!(error.contains("pseudolife-mcp serve"), "{error}");
    let sql = |statement: &str| lite.embedded.query(&lite.runtime, statement).unwrap();
    sql("CREATE TABLE meta (key text PRIMARY KEY, value jsonb)");
    sql("CREATE TABLE entries (id bigint)");
    sql("CREATE TABLE facts (id bigint)");
    // Empty tables, no record: still refused.
    assert!(legacy_state(&lite).is_err());
    // A bank with entries and no import under way: served (Python skips).
    sql("INSERT INTO entries VALUES (1)");
    assert_eq!(legacy_state(&lite), Ok(()));
    // An import in progress: refused even with entries.
    sql("INSERT INTO meta VALUES ('legacy_migration', '{\"status\": \"in_progress\"}')");
    assert!(legacy_state(&lite).is_err());
    // Done: served.
    sql("UPDATE meta SET value = '{\"status\": \"done\"}' WHERE key = 'legacy_migration'");
    assert_eq!(legacy_state(&lite), Ok(()));
    stop_lite(lite);
    let _ = fs::remove_dir_all(data_dir);
}
