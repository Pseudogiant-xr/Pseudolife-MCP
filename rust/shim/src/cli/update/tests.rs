use super::*;

fn verify_bank_identity(config: &Value, running: &Value) -> Result<()> {
    engine::verify_bank_identity_with(config, running, |name| {
        Some(json!([
            {"Name":name,"Labels":{"com.docker.volume.anonymous":""}}
        ]))
    })
}
use std::io::{Cursor, Read, Seek, SeekFrom, Write};

#[cfg(windows)]
#[test]
fn native_update_external_paths_keep_drive_unc_and_unicode_without_verbatim_prefixes() {
    for (input, expected) in [
        (
            r"\\?\C:\checkout λ\ops\backup.ps1",
            r"C:\checkout λ\ops\backup.ps1",
        ),
        (
            r"\\?\UNC\server\share\checkout λ",
            r"\\server\share\checkout λ",
        ),
        (r"C:\checkout\ops", r"C:\checkout\ops"),
    ] {
        assert_eq!(seams::command_path(input.into()), PathBuf::from(expected));
    }
}
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "native-update-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn gzip(bytes: &[u8]) -> Vec<u8> {
    let mut gz = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
    gz.write_all(bytes).unwrap();
    gz.finish().unwrap()
}
fn archive(files: &[(&str, &[u8])]) -> Vec<u8> {
    let gz = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
    let mut tar = tar::Builder::new(gz);
    for (name, bytes) in files {
        let mut h = tar::Header::new_gnu();
        h.set_size(bytes.len() as u64);
        h.set_mode(0o644);
        h.set_cksum();
        tar.append_data(&mut h, name, Cursor::new(bytes)).unwrap();
    }
    tar.into_inner().unwrap().finish().unwrap()
}
fn sums(bytes: &[u8]) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    format!(
        "{:x}  pseudolife-compose-0.21.0.tar.gz\n",
        Sha256::digest(bytes)
    )
    .into_bytes()
}

#[test]
fn native_update_plain_versions_reject_requirements() {
    for v in ["0.21.0", "1.0", "1.2.3.4"] {
        assert!(plain_release(v));
    }
    for v in ["1", "v1.0", "1.0rc1", "1.0+local", "1.2; rm", "١.٢", "1..2"] {
        assert!(!plain_release(v));
    }
}
#[test]
fn native_update_option_refusals_precede_effects() {
    for args in [
        vec!["--clients-only", "--daemon-only"],
        vec!["--shadow"],
        vec!["--checkout", "repo", "--tag", "1.2.3"],
        vec!["--unattended", "--no-backup"],
        vec!["--tag", "1.2;bad"],
        vec!["--rollback-tag", "bad/tag"],
        vec!["--no-test-login"],
        vec!["--engine", "rust"],
    ] {
        assert_eq!(
            Options::parse(args.into_iter().map(OsString::from).collect())
                .unwrap_err()
                .exit_code,
            2
        );
    }
}
#[test]
fn native_update_engine_is_opt_in() {
    assert!(Options::default().engine.is_none());
    let options = Options::parse(
        ["--engine", "rust", "--checkout", "repo", "--shadow"]
            .into_iter()
            .map(OsString::from)
            .collect(),
    )
    .unwrap();
    assert_eq!(options.engine.as_deref(), Some("rust"));
    assert!(options.shadow);
}
#[test]
fn native_update_lock_excludes_and_releases() {
    let temp = Temp::new();
    let mut first = os_lock::Lock::new(temp.0.join("update.lock"));
    let mut second = os_lock::Lock::new(temp.0.join("update.lock"));
    assert!(matches!(first.acquire(), Ok(true)));
    assert!(matches!(second.acquire(), Ok(false)));
    first.release();
    assert!(matches!(second.acquire(), Ok(true)));
    second.release();
}
#[test]
fn native_update_dump_marker_outside_copy_only() {
    let temp = Temp::new();
    let path = temp.0.join("dump.gz");
    fs::write(
        &path,
        gzip(
            b"COPY public.entries (body) FROM stdin;\n-- PostgreSQL database dump complete\n\\.\n",
        ),
    )
    .unwrap();
    assert!(!backup::dump_complete(&path));
    fs::write(&path,gzip(b"COPY public.entries (body) FROM stdin;\nrow\n\\.\n-- PostgreSQL database dump complete\n")).unwrap();
    assert!(backup::dump_complete(&path));
    let mut broken = fs::read(&path).unwrap();
    broken.pop();
    fs::write(&path, broken).unwrap();
    assert!(!backup::dump_complete(&path));
}
#[test]
fn native_update_backup_requires_changed_complete_pair() {
    let temp = Temp::new();
    let before = backup::snapshot(&temp.0);
    let dump = temp.0.join("pseudolife_memory-20260101-000001.sql.gz");
    fs::write(&dump, gzip(b"-- PostgreSQL database dump complete\n")).unwrap();
    assert!(backup::verify(&temp.0, &before).is_err());
    let state = temp.0.join("pseudolife_state-20260101-000001.tgz");
    fs::write(&state, archive(&[("state.json", b"{}")])).unwrap();
    assert!(backup::verify(&temp.0, &before).is_ok());
    assert!(backup::verify(&temp.0, &backup::snapshot(&temp.0)).is_err());
    fs::write(&state, gzip(b"not a tar")).unwrap();
    assert!(backup::verify(&temp.0, &before).is_err());
}
#[test]
fn native_update_compose_requires_checksum_and_closed_archive() {
    let files = [
        ("docker-compose.yml", b"services: {}".as_slice()),
        ("docker-compose.ghcr.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-daemon.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-shadow.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-ghcr.yml", b"services: {}".as_slice()),
        ("Dockerfile.extractor", b"FROM scratch".as_slice()),
    ];
    let bytes = archive(&files);
    assert_eq!(
        release::verified_compose("0.21.0", &sums(&bytes), &bytes)
            .unwrap()
            .len(),
        6
    );
    let mut corrupt = bytes.clone();
    corrupt[8] ^= 1;
    assert!(release::verified_compose("0.21.0", &sums(&bytes), &corrupt).is_err());
    let incomplete = archive(&files[..4]);
    assert!(release::verified_compose("0.21.0", &sums(&incomplete), &incomplete).is_err());
    let mut extra = files.to_vec();
    extra.push(("unexpected", b"x"));
    let bytes = archive(&extra);
    assert!(release::verified_compose("0.21.0", &sums(&bytes), &bytes).is_err());
    extra.pop();
    extra.push(files[0]);
    let bytes = archive(&extra);
    assert!(release::verified_compose("0.21.0", &sums(&bytes), &bytes).is_err());
}
#[test]
fn native_update_null_labels_and_failed_activation_are_distinct() {
    let temp = Temp::new();
    let accepted = temp.0.join(engine::SELECTION);
    let service = json!({"image":"fixture:accepted","labels":{(engine::ENGINE_LABEL):"rust",(engine::CHECKOUT_LABEL):temp.0,(engine::ACTIVATION_LABEL):"accepted"}});
    engine::write_overlay(&accepted, &service).unwrap();
    assert!(engine::validated(&service["labels"]).is_ok());
    let mut failed = service["labels"].clone();
    failed[engine::ACTIVATION_LABEL] = json!("failed");
    assert!(
        engine::validated(&failed)
            .unwrap_err()
            .message
            .contains("rollback")
    );
    assert!(engine::selection(&temp.0.join("absent")).unwrap().is_none());
    fs::write(&accepted, b"null").unwrap();
    assert!(engine::selection(&accepted).is_err());
    let running = json!({"Config":{"Labels":null}});
    assert_eq!(
        engine::rollback_service(&running, "fixture:rollback", None)["labels"]
            [engine::ENGINE_LABEL],
        "python"
    );
}
#[test]
fn native_update_mapped_bearer_preserves_colons() {
    let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_TOKENS":" malformed, :reader, reserved:default, fixture:colon : Reader , next:other "}}}});
    assert!(engine::daemon_token(&config).is_ok_and(|v| v == "fixture:colon"));
}

#[test]
fn native_update_identity_checks_every_effective_mount() {
    let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"},
        {"type":"bind","source":"/original/config.yaml","target":"/data/config.yaml","read_only":true}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"bind","Source":"/original/config.yaml","Destination":"/data/config.yaml","RW":false}
    ]});
    assert!(verify_bank_identity(&config, &running).is_ok());
    config["services"][DAEMON_SERVICE]["volumes"][1]["source"] = json!("/bundle/config.yaml");
    assert!(verify_bank_identity(&config, &running).is_err());
    config["services"][DAEMON_SERVICE]["volumes"][1]["source"] = json!("/original/config.yaml");
    config["services"][DAEMON_SERVICE]["volumes"][1]["read_only"] = json!(false);
    assert!(verify_bank_identity(&config, &running).is_err());
    config["services"][DAEMON_SERVICE]["volumes"]
        .as_array_mut()
        .unwrap()
        .pop();
    assert!(verify_bank_identity(&config, &running).is_err());
}

#[test]
fn native_update_mount_identity_normalizes_windows_docker_desktop_sources() {
    for actual in [
        r"c:\fixture\config.yaml",
        "c:/fixture/config.yaml",
        "/run/desktop/mnt/host/c/fixture/config.yaml",
        "//./C:/fixture/config.yaml",
        r"\\?\C:\fixture\config.yaml",
    ] {
        let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
            {"type":"volume","source":"state","target":"/data"},
            {"type":"bind","source":r"C:\fixture\config.yaml","target":"/config","read_only":true}
        ]}},"volumes":{"state":{"name":"fixture-state"}}});
        let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
            {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
            {"Type":"bind","Source":actual,"Destination":"/config","RW":false}
        ]});
        assert!(verify_bank_identity(&config, &running).is_ok(), "{actual}");
        running["Mounts"][1]["Source"] = json!("/run/desktop/mnt/host/d/fixture/config.yaml");
        assert!(verify_bank_identity(&config, &running).is_err());
    }
}

#[test]
fn native_update_mount_identity_ignores_tmpfs_and_sourceless_mounts() {
    let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"},
        {"type":"volume","target":"/cache"},
        {"type":"tmpfs","target":"/tmp"}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache"}]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"volume","Name":"generated-anonymous","Destination":"/cache","RW":true},
        {"Type":"tmpfs","Destination":"/tmp","RW":true},
        {"Type":"tmpfs","Destination":"/other-tmp","RW":true},
        {"Type":"bind","Destination":"/sourceless","RW":true}
    ]});
    assert!(verify_bank_identity(&config, &running).is_ok());
}

#[test]
fn native_update_ephemeral_mount_cannot_hide_persistent_contents() {
    for previous in ["bind", "volume"] {
        for replacement in ["tmpfs", "volume"] {
            let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
                {"type":"volume","source":"state","target":"/data"},
                {"type":replacement,"target":"/cache"}
            ]}},"volumes":{"state":{"name":"fixture-state"}}});
            let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
                {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
                {"Type":previous,"Name":"persistent-cache","Source":"/persistent/cache","Destination":"/cache","RW":false}
            ]});
            let refusal = verify_bank_identity(&config, &running).unwrap_err();
            assert!(refusal.message.contains("/cache") && refusal.message.contains("restore"));
            running["Mounts"][1] = json!({"Type":"tmpfs","Destination":"/cache"});
            assert!(verify_bank_identity(&config, &running).is_ok());
            running["Mounts"].as_array_mut().unwrap().pop();
            assert!(verify_bank_identity(&config, &running).is_ok());
        }
    }
}
#[test]
fn native_update_anonymous_volume_survives_second_recreation() {
    let name = "a".repeat(64);
    let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"},
        {"type":"volume","target":"/cache"}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache"}]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"volume","Name":name,"Source":format!("/var/lib/docker/volumes/{name}/_data"),"Destination":"/cache","RW":true}
    ]});
    assert!(verify_bank_identity(&config, &running).is_ok());
    // Compose inherits the anonymous volume on the first recreate. The next
    // update sees its generated name in HostConfig, not an empty Source.
    running["HostConfig"]["Mounts"][0]["Source"] = json!(name);
    for _generation in [2, 3] {
        assert!(verify_bank_identity(&config, &running).is_ok());
    }
    config["volumes"]["cache"] = json!({"name":name});
    assert!(verify_bank_identity(&config, &running).is_err());
    config["volumes"].as_object_mut().unwrap().remove("cache");
    running["HostConfig"]["Mounts"][0]["Source"] = json!("different-volume");
    assert!(verify_bank_identity(&config, &running).is_err());
    running["HostConfig"] = Value::Null;
    assert!(verify_bank_identity(&config, &running).is_err());
}

#[test]
fn native_update_external_hash_volume_requires_anonymous_provenance() {
    let name = "a".repeat(64);
    let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"}, {"type":"tmpfs","target":"/cache"}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache","Source":name}]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"volume","Name":name,"Source":format!("/var/lib/docker/volumes/{name}/_data"),"Destination":"/cache","RW":true}
    ]});
    for metadata in [
        None,
        Some(json!([])),
        Some(json!({})),
        Some(json!([{"Name":"other-volume","Labels":{"com.docker.volume.anonymous":""}}])),
        Some(json!([{"Name":name,"Labels":{}}])),
        Some(json!([{"Name":name,"Labels":null}])),
        Some(json!([{"Name":name,"Labels":{"com.docker.volume.anonymous":true}}])),
    ] {
        let error =
            engine::verify_bank_identity_with(&config, &running, |_| metadata.clone()).unwrap_err();
        assert!(
            error.message.contains("/cache")
                && error.message.contains("declare")
                && error.message.contains("recreate")
        );
    }
}

#[test]
fn native_update_anonymous_volume_requires_name_and_resolved_named_set() {
    for missing_name in [true, false] {
        let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
            {"type":"volume","source":"state","target":"/data"}, {"type":"volume","target":"/cache"}
        ]}},"volumes":{"state":{"name":"fixture-state"}}});
        let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache"}]},"Mounts":[
            {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
            {"Type":"volume","Name":"a".repeat(64),"Destination":"/cache","RW":true}
        ]});
        if missing_name {
            running["Mounts"][1].as_object_mut().unwrap().remove("Name");
        } else {
            config["volumes"]["unresolved"] = json!({});
        }
        assert!(
            verify_bank_identity(&config, &running).is_err(),
            "missing_name={missing_name}"
        );
    }
}

#[test]
fn native_update_rollback_records_commit_after_recreation() {
    let temp = Temp::new();
    let commands = engine::rollback_commands(
        &[],
        &[],
        &temp.0.join("rollback.json"),
        &temp.0.join("accepted.json"),
        Some(&temp.0.join("selected.json")),
        Some("1.2.3"),
    );
    let command = commands.join("\n");
    let recreate = command.find("up -d --no-deps --no-build").unwrap();
    let copy = command
        .find(if cfg!(windows) { "Copy-Item" } else { "cp --" })
        .unwrap();
    let remove = command
        .find(if cfg!(windows) {
            "Remove-Item -LiteralPath"
        } else {
            "rm -f --"
        })
        .unwrap();
    assert!(recreate < copy && copy < remove);
    assert!(command.contains("Restore PSEUDOLIFE_MCP_TOKEN"));
    assert!(command.contains(if cfg!(windows) {
        "$LASTEXITCODE -eq 0"
    } else {
        "&&"
    }));
    assert!(command.contains("--pull never"));
    let tag = command.find("PSEUDOLIFE_IMAGE_TAG=").unwrap();
    assert!(tag < command.find("docker compose").unwrap());
    assert!(command[tag..].contains("1.2.3"));
}
struct FaultFile {
    data: Cursor<Vec<u8>>,
    limit: usize,
    fail_after: usize,
    writes: usize,
    fail_sync: bool,
}
impl Read for FaultFile {
    fn read(&mut self, b: &mut [u8]) -> std::io::Result<usize> {
        self.data.read(b)
    }
}
impl Seek for FaultFile {
    fn seek(&mut self, s: SeekFrom) -> std::io::Result<u64> {
        self.data.seek(s)
    }
}
impl Write for FaultFile {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.writes += 1;
        if self.writes > self.fail_after {
            return Err(std::io::Error::from_raw_os_error(28));
        }
        self.data.write(&b[..b.len().min(self.limit)])
    }
    fn flush(&mut self) -> std::io::Result<()> {
        panic!("buffered flush after ENOSPC is forbidden")
    }
}
impl shadow::AppendFile for FaultFile {
    fn truncate(&mut self, len: u64) -> std::io::Result<()> {
        self.data.get_mut().truncate(len as usize);
        Ok(())
    }
    fn sync(&mut self) -> std::io::Result<()> {
        if self.fail_sync {
            self.fail_sync = false;
            Err(std::io::Error::from_raw_os_error(28))
        } else {
            Ok(())
        }
    }
}
#[test]
fn native_update_enospc_append_restores_bytes_without_retry() {
    for limit in [0, 3, usize::MAX] {
        let original = b"# foreign owner and access retained\r\n";
        let mut file = FaultFile {
            data: Cursor::new(original.to_vec()),
            limit,
            fail_after: if limit == usize::MAX { 0 } else { 1 },
            writes: 0,
            fail_sync: false,
        };
        assert!(shadow::append_checked(&mut file, original, b"\nPASSWORD=fixture\n").is_err());
        assert_eq!(file.data.get_ref(), original);
        assert_eq!(file.writes, if limit == 3 { 2 } else { 1 });
    }
}
#[test]
fn native_update_short_writes_and_sync_failure_are_recoverable() {
    let original = b"existing\r\n";
    let mut file = FaultFile {
        data: Cursor::new(original.to_vec()),
        limit: 3,
        fail_after: usize::MAX,
        writes: 0,
        fail_sync: false,
    };
    shadow::append_checked(&mut file, original, b"added").unwrap();
    assert_eq!(file.data.get_ref(), b"existing\r\nadded");
    let original = file.data.get_ref().clone();
    file.fail_sync = true;
    assert!(shadow::append_checked(&mut file, &original, b"not-committed").is_err());
    assert_eq!(file.data.get_ref(), &original);
    shadow::append_checked(&mut file, &original, b"retry").unwrap();
    assert!(file.data.get_ref().ends_with(b"retry"));
}
#[test]
fn native_update_intervening_env_edits_survive() {
    let mut file = FaultFile {
        data: Cursor::new(b"intervening".to_vec()),
        limit: 3,
        fail_after: usize::MAX,
        writes: 0,
        fail_sync: false,
    };
    assert!(shadow::append_checked(&mut file, b"original", b"addition").is_err());
    assert_eq!(file.data.get_ref(), b"intervening");
    assert_eq!(file.writes, 0);
}

#[test]
fn native_update_panic_persists_failure_and_clears_stale_codex_result() {
    let temp = Temp::new();
    let path = temp.0.join("result");
    seams::write_atomic(&path, b"0\n").unwrap();
    seams::write_atomic(&path.with_extension("codex"), b"stale approval\n").unwrap();
    let mut update = Update::new(Options {
        result_file: Some(path.clone()),
        as_json: true,
        ..Options::default()
    });
    let caught = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        update.run_with(|_| panic!("synthetic transaction panic"))
    }));
    assert!(caught.is_err());
    assert_eq!(fs::read(&path).unwrap(), b"1\n");
    assert!(!path.with_extension("codex").exists());
    assert!(!update.report.ok);
    let mut retry = Update::new(Options {
        result_file: Some(path.clone()),
        as_json: true,
        ..Options::default()
    });
    assert_eq!(retry.run_with(|_| Ok(())), 0);
    assert_eq!(fs::read(&path).unwrap(), b"0\n");
}

#[test]
fn native_update_result_write_failure_preserves_primary_error_and_retry_needs_no_state_repair() {
    let temp = Temp::new();
    let blocker = temp.0.join("blocked");
    fs::write(&blocker, b"owned fixture").unwrap();
    let path = blocker.join("result");
    let options = Options {
        result_file: Some(path.clone()),
        as_json: true,
        ..Options::default()
    };
    let mut update = Update::new(options.clone());
    assert_eq!(
        update.run_with(|_| Err(UpdateError::usage("synthetic preflight refusal"))),
        2
    );
    assert_eq!(fs::read(&blocker).unwrap(), b"owned fixture");
    assert!(
        update
            .report
            .steps
            .iter()
            .any(|line| line.contains("could not write the update result file"))
    );
    fs::remove_file(&blocker).unwrap();
    fs::create_dir(&blocker).unwrap();
    let mut retry = Update::new(options);
    assert_eq!(retry.run_with(|_| Ok(())), 0);
    assert_eq!(fs::read(path).unwrap(), b"0\n");
}

#[test]
fn native_update_compose_publication_is_atomic_immutable_and_keeps_prior_versions() {
    let temp = Temp::new();
    let root = temp.0.join("compose");
    let files = [("docker-compose.yml".into(), b"verified first".to_vec())]
        .into_iter()
        .collect();
    let first = release::publish_compose(&root, "0.21.0", &files).unwrap();
    assert_eq!(
        fs::read(first.join("docker-compose.yml")).unwrap(),
        b"verified first"
    );
    assert_eq!(
        release::publish_compose(&root, "0.21.0", &files).unwrap(),
        first
    );
    let changed = [(
        "docker-compose.yml".into(),
        b"different release bytes".to_vec(),
    )]
    .into_iter()
    .collect();
    assert!(release::publish_compose(&root, "0.21.0", &changed).is_err());
    assert_eq!(
        fs::read(first.join("docker-compose.yml")).unwrap(),
        b"verified first"
    );
    assert!(
        release::publish_compose(&root, "0.21.1", &changed)
            .unwrap()
            .is_dir()
    );
    assert!(first.is_dir());
    assert_eq!(fs::read_dir(root).unwrap().count(), 2);
}

#[test]
fn maintainer_check_and_interactive_setup_keep_the_selected_target() {
    let mut update = Update::new(Options::default());
    update.o.daemon_url = "http://127.0.0.1:9876".into();
    for (check, flag) in [(true, "--check"), (false, "--yes")] {
        assert_eq!(
            update.maintainer_command(Path::new("native-cli"), "second-project-new-daemon", check),
            [
                "native-cli",
                "maintainer",
                "setup",
                flag,
                "--container",
                "second-project-new-daemon",
                "--port",
                "9876"
            ]
        );
    }
}
