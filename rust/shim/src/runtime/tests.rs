use super::*;
use std::collections::BTreeSet;
#[cfg(not(windows))]
use std::io::Write;

struct Fixture {
    path: PathBuf,
    layout: Layout,
}
impl Fixture {
    fn new() -> Self {
        let path =
            std::env::temp_dir().join(format!("native-runtime-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        let layout = Layout {
            root: path.join("runtimes"),
            launcher: path.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        };
        Self { path, layout }
    }
    fn binary(&self, version: &str) -> PathBuf {
        let path = self.path.join(format!("fixture-{version}"));
        #[cfg(windows)]
        let header = b"MZ".as_slice();
        #[cfg(target_os = "linux")]
        let header = b"\x7fELF".as_slice();
        #[cfg(target_os = "macos")]
        let header = b"\xcf\xfa\xed\xfe".as_slice();
        let mut bytes = header.to_vec();
        bytes.extend_from_slice(version.as_bytes());
        fs::write(&path, bytes).unwrap();
        path
    }
    fn install(&self, version: &str) -> Runtime {
        let _lock = UpdateLock::acquire(&self.layout).unwrap();
        install_binary(
            &self.layout,
            version,
            &format!("pseudolife-mcp=={version}"),
            None,
            &self.binary(version),
        )
        .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}
fn evidence() -> Evidence {
    Evidence {
        processes: vec![],
        registrations: BTreeSet::new(),
        tunnels: BTreeSet::new(),
    }
}

#[test]
fn incomplete_and_forged_markers_never_select() {
    let f = Fixture::new();
    let first = f.install("1.0.0");
    let half = f.layout.root.join("000002");
    fs::create_dir(&half).unwrap();
    fs::write(half.join(BINARY), b"incomplete").unwrap();
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, 1);
    crate::board::state::atomic_create(
        &half.join(MARKER),
        br#"{"version":"999.0.0","kind":"native"}"#,
    )
    .unwrap();
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, 1);
    let mut forged = first.marker.clone();
    forged.binary_sha256 = "0".repeat(64);
    crate::board::state::atomic_write(
        &first.path.join(MARKER),
        &serde_json::to_vec(&forged).unwrap(),
        None,
    )
    .unwrap();
    assert!(current_runtime(&f.layout).is_none());
}
#[test]
fn marker_cannot_select_a_modified_binary() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let new = f.install("1.0.1");
    fs::write(new.executable(), b"changed after install").unwrap();
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, old.sequence);
    let (selected, _pin) = launch::select_pin_with(&f.layout, launch::pin).unwrap();
    assert_eq!(selected.sequence, old.sequence);
}
#[test]
fn legacy_python_runtime_is_never_selected_or_deleted() {
    let f = Fixture::new();
    let native = f.install("1.0.0");
    let legacy = f.layout.root.join("999999");
    fs::create_dir(&legacy).unwrap();
    crate::board::state::atomic_create(
        &legacy.join(MARKER),
        br#"{"version":"9.0.0","base_interpreter":"python"}"#,
    )
    .unwrap();
    assert_eq!(
        current_runtime(&f.layout).unwrap().sequence,
        native.sequence
    );
    remove_unused(&f.layout, Ok(evidence()));
    assert!(legacy.is_dir());
}
#[test]
fn install_is_side_by_side_and_skips_incomplete_sequence() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    fs::create_dir(f.layout.root.join("000009")).unwrap();
    let new = f.install("1.0.1");
    assert_eq!(new.sequence, 10);
    assert!(old.executable().is_file());
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, 10);
}
#[test]
fn invalid_archive_checksum_changes_no_runtime_or_launcher() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let launcher = fs::read(&f.layout.launcher).unwrap();
    let name = install::archive_name("1.0.1").unwrap();
    let error = install_archive(
        &f.layout,
        "1.0.1",
        "pseudolife-mcp==1.0.1",
        b"wrong archive",
        &format!("{}  {name}\n", "0".repeat(64)),
    )
    .unwrap_err();
    assert!(error.contains("checksum mismatch"));
    assert_eq!(fs::read(&f.layout.launcher).unwrap(), launcher);
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, old.sequence);
}
#[test]
fn checksums_require_exact_lowercase_unique_asset_record() {
    let name = install::archive_name("1.2.3").unwrap();
    assert!(install::checksum(&format!("{}  {name}\n", "A".repeat(64)), &name).is_err());
    let line = format!("{}  {name}\n", "a".repeat(64));
    assert!(install::checksum(&format!("{line}{line}"), &name).is_err());
    assert!(install::checksum(&line, "other.tar.gz").is_err());
    assert_eq!(install::checksum(&line, &name).unwrap(), "a".repeat(64));
}
#[test]
fn dropped_activation_restores_old_launcher() {
    let f = Fixture::new();
    f.install("1.0.0");
    let before = fs::read(&f.layout.launcher).unwrap();
    {
        let _activation = install::activate_launcher(&f.layout, &f.binary("1.0.1")).unwrap();
        assert_ne!(fs::read(&f.layout.launcher).unwrap(), before);
    }
    assert_eq!(fs::read(&f.layout.launcher).unwrap(), before);
    assert_eq!(current_runtime(&f.layout).unwrap().version, "1.0.0");
}
#[test]
fn activation_invalidates_python_launcher_record_and_restores_it_on_rollback() {
    let f = Fixture::new();
    f.install("1.0.0");
    let mut record = f.layout.launcher.as_os_str().to_os_string();
    record.push(".json");
    let record = PathBuf::from(record);
    let cached = br#"{"root":"legacy","base_interpreter":"python","selector_sha256":"old"}"#;
    fs::write(&record, cached).unwrap();
    {
        let _activation = install::activate_launcher(&f.layout, &f.binary("1.0.1")).unwrap();
        assert!(
            !record.exists(),
            "Python must not mistake a native launcher for its cached stub"
        );
    }
    assert_eq!(fs::read(&record).unwrap(), cached);
    f.install("1.0.1");
    assert!(!record.exists());
    assert!(
        fs::read_dir(f.layout.launcher.parent().unwrap())
            .unwrap()
            .all(|e| {
                !e.unwrap()
                    .file_name()
                    .to_string_lossy()
                    .contains(".old-native-")
            })
    );
}

#[test]
fn committed_activation_removes_displaced_launcher_copy() {
    let f = Fixture::new();
    f.install("1.0.0");
    f.install("1.0.1");
    assert_eq!(
        fs::read_dir(f.layout.launcher.parent().unwrap())
            .unwrap()
            .count(),
        1
    );
}
#[test]
fn failed_activation_publishes_no_completion_marker() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let mut broken = f.layout.clone();
    broken.launcher = f.path.join("occupied");
    fs::create_dir(&broken.launcher).unwrap();
    assert!(install_binary(&broken, "1.0.1", "fixture", None, &f.binary("1.0.1")).is_err());
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, old.sequence);
    assert!(!f.layout.root.join("000002").exists());
}
#[test]
fn unknown_process_registration_or_tunnel_evidence_prunes_nothing() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    for reason in [
        "process table unreadable",
        "registration unreadable",
        "tunnel snapshot unreadable",
    ] {
        let report = remove_unused(&f.layout, Err(reason.into()));
        assert_eq!(report["removed"], json!([]));
        assert_eq!(report["error"], reason);
        assert!(old.path.is_dir());
    }
}
#[test]
fn running_registered_and_tunnel_pins_survive_until_idle() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let new = f.install("1.0.1");
    let mut e = evidence();
    e.processes.push((42, 1, old.executable()));
    assert_eq!(remove_unused(&f.layout, Ok(e))["held"][0]["processes"], 1);
    let mut e = evidence();
    e.registrations.insert(old.path.clone());
    remove_unused(&f.layout, Ok(e));
    assert!(old.path.is_dir());
    let mut e = evidence();
    e.tunnels.insert(old.path.clone());
    remove_unused(&f.layout, Ok(e));
    assert!(old.path.is_dir());
    let report = remove_unused(&f.layout, Ok(evidence()));
    assert_eq!(report["removed"], json!([old.path]), "{report}");
    assert!(new.path.is_dir());
}
#[test]
fn usage_lock_protects_runtime_even_with_stale_process_evidence() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let lock = crate::board::state::create(&old.usage_lock_path()).unwrap();
    lock.file.lock_shared().unwrap();
    assert!(
        remove_unused(&f.layout, Ok(evidence()))["removed"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(old.path.is_dir());
    drop(lock);
    let report = remove_unused(&f.layout, Ok(evidence()));
    assert_eq!(report["removed"], json!([old.path]), "{report}");
}
#[test]
fn update_lock_excludes_competing_installers_and_recovers_on_drop() {
    let f = Fixture::new();
    let lock = UpdateLock::acquire(&f.layout).unwrap();
    assert!(UpdateLock::acquire(&f.layout).is_err());
    drop(lock);
    assert!(UpdateLock::acquire(&f.layout).is_ok());
}
#[test]
fn legacy_frozen_bridge_pins_its_runtime_and_malformed_evidence_refuses() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    f.install("1.0.1");
    let root = f.path.join("tunnel");
    let digest = "a".repeat(64);
    let bridge = root.join(".bridges").join(&digest);
    crate::board::state::private_dir(&bridge).unwrap();
    let dependency = old.path.join("lib/site-packages");
    let line = format!(
        "sys.path[1:1]=[{}]\n",
        serde_json::to_string(&dependency).unwrap()
    );
    crate::board::state::atomic_create(&bridge.join("launch.py"), line.as_bytes()).unwrap();
    let record = json!({"pid":42,"snapshot":{"digest":digest,"interpreter":"legacy"}});
    crate::board::state::atomic_create(
        &root.join("fixture.process.json"),
        &serde_json::to_vec(&record).unwrap(),
    )
    .unwrap();
    assert!(
        prune::tunnel_runtimes(&f.layout, &root, &[(42, 1, f.path.join("other"))])
            .unwrap()
            .contains(&old.path)
    );
    assert!(
        prune::tunnel_runtimes(&f.layout, &root, &[])
            .unwrap()
            .is_empty()
    );
    crate::board::state::atomic_write(
        &bridge.join("launch.py"),
        b"sys.path[1:1]=__import__('os')",
        None,
    )
    .unwrap();
    assert!(prune::tunnel_runtimes(&f.layout, &root, &[(42, 1, f.path.join("other"))]).is_err());
}
#[test]
fn registration_edit_preserves_unicode_unrelated_data_and_module_tail() {
    let f = Fixture::new();
    let path = f.path.join("client.json");
    let before = json!({"theme":"日本語","mcpServers":{"other":{"command":"other"},registrations::SERVER:{"command":f.layout.root.join("000001/python"),"args":["-m","pseudolife_memory.cli","channel"],"cwd":"old","env":{"PSEUDOLIFE_MCP_NO_SPAWN":"1","fixture":"unchanged"}}}});
    crate::board::state::atomic_create(&path, &serde_json::to_vec(&before).unwrap()).unwrap();
    let r = registrations::Registration {
        client: "gemini".into(),
        file: path.clone(),
        key: registrations::SERVER.into(),
        command: f.layout.root.join("000001/python").to_string_lossy().into(),
        args: vec![
            "-m".into(),
            "pseudolife_memory.cli".into(),
            "channel".into(),
        ],
        cwd: Some("old".into()),
        env: before["mcpServers"][registrations::SERVER]["env"].clone(),
    };
    let report = registrations::migrate(&r, &f.layout);
    assert_eq!(report["state"], "migrated");
    let after: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(after["theme"], before["theme"]);
    assert_eq!(after["mcpServers"]["other"], before["mcpServers"]["other"]);
    assert_eq!(
        after["mcpServers"][registrations::SERVER]["args"],
        json!(["channel"])
    );
    assert_eq!(
        after["mcpServers"][registrations::SERVER]["env"],
        before["mcpServers"][registrations::SERVER]["env"]
    );
    assert!(
        !after["mcpServers"][registrations::SERVER]
            .as_object()
            .unwrap()
            .contains_key("cwd")
    );
}
#[test]
fn changed_registration_fields_are_not_overwritten_by_a_stale_discovery() {
    let f = Fixture::new();
    for client in ["gemini", "codex"] {
        for field in ["command", "args", "cwd", "env"] {
            let path = f.path.join(format!("{client}-{field}.config"));
            let old =
                json!({"command":"old", "args":["shim"], "cwd":"old", "env":{"setting":"old"}});
            let mut new = old.clone();
            new[field] = match field {
                "args" => json!(["channel"]),
                "env" => json!({"setting":"new"}),
                _ => json!("new"),
            };
            let bytes = if client == "codex" {
                format!("[mcp_servers.pseudolife-memory]\ncommand = {}\nargs = {}\ncwd = {}\n[mcp_servers.pseudolife-memory.env]\nsetting = {}\n",
                    new["command"], new["args"], new["cwd"], new["env"]["setting"]).into_bytes()
            } else {
                serde_json::to_vec(&json!({"mcpServers":{registrations::SERVER:new}})).unwrap()
            };
            crate::board::state::atomic_create(&path, &bytes).unwrap();
            let registration = registrations::Registration {
                client: client.into(),
                file: path.clone(),
                key: registrations::SERVER.into(),
                command: "old".into(),
                args: vec!["shim".into()],
                cwd: Some("old".into()),
                env: old["env"].clone(),
            };
            let report = registrations::migrate(&registration, &f.layout);
            assert_eq!(report["state"], "failed", "{client} {field}: {report}");
            assert_eq!(fs::read(&path).unwrap(), bytes);
        }
    }
}
#[test]
fn concurrent_config_change_is_preserved_with_private_backup() {
    let f = Fixture::new();
    let path = f.path.join("config.json");
    crate::board::state::atomic_create(&path, b"new writer content").unwrap();
    assert!(registrations::write_config(&path, b"old content", b"replacement").is_err());
    assert_eq!(fs::read(&path).unwrap(), b"new writer content");
}
#[test]
fn half_set_layout_override_is_an_error_and_custom_layout_exposes_nothing() {
    let f = Fixture::new();
    assert!(
        layout_from(|k| (k == "PSEUDOLIFE_SHIM_RUNTIMES").then(|| f.layout.root.clone())).is_err()
    );
    let layout = layout_from(|k| match k {
        "PSEUDOLIFE_SHIM_RUNTIMES" => Some(f.layout.root.clone()),
        "PSEUDOLIFE_SHIM_LAUNCHER" => Some(f.layout.launcher.clone()),
        _ => None,
    })
    .unwrap();
    assert!(layout.user_bin.is_none());
    assert!(!layout.user_path);
}

#[cfg(not(windows))]
#[test]
fn archive_links_traversal_extra_files_and_bombs_are_rejected() {
    use flate2::{Compression, write::GzEncoder};
    let f = Fixture::new();
    for (name, kind, size) in [
        (BINARY, tar::EntryType::Symlink, 0),
        ("../pseudolife-mcp", tar::EntryType::Regular, 0),
        (BINARY, tar::EntryType::Regular, 300 * 1024 * 1024),
    ] {
        let mut bytes = Vec::new();
        {
            let encoder = GzEncoder::new(&mut bytes, Compression::fast());
            let mut archive = tar::Builder::new(encoder);
            let mut header = tar::Header::new_gnu();
            header.set_entry_type(kind);
            header.set_mode(0o755);
            header.set_size(size);
            // Write raw path bytes to exercise traversal refusal, independently
            // of tar::Builder's own safe-path admission.
            header.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
            header.set_cksum();
            archive.get_mut().write_all(header.as_bytes()).unwrap();
            archive.finish().unwrap();
            archive.into_inner().unwrap().finish().unwrap();
        }
        let digest = format!("{:x}", Sha256::digest(&bytes));
        let name = install::archive_name("1.0.0").unwrap();
        assert!(
            install_archive(
                &f.layout,
                "1.0.0",
                "fixture",
                &bytes,
                &format!("{digest}  {name}\n")
            )
            .is_err()
        );
    }
    assert!(current_runtime(&f.layout).is_none());
}

#[cfg(windows)]
#[test]
fn locked_windows_launcher_keeps_old_runtime_and_allows_next_retry() {
    use std::os::windows::fs::OpenOptionsExt;
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let before = fs::read(&f.layout.launcher).unwrap();
    let handle = fs::OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(&f.layout.launcher)
        .unwrap();
    assert!(install_binary(&f.layout, "1.0.1", "fixture", None, &f.binary("1.0.1")).is_err());
    drop(handle);
    assert_eq!(fs::read(&f.layout.launcher).unwrap(), before);
    assert_eq!(current_runtime(&f.layout).unwrap().sequence, old.sequence);
    assert_eq!(f.install("1.0.1").version, "1.0.1");
}

#[test]
fn another_first_launcher_can_create_the_usage_file_before_our_create() {
    let f = Fixture::new();
    let runtime = f.install("1.0.0");
    let path = runtime.usage_lock_path();
    let lock = launch::usage_file_with(&path, || {
        drop(crate::board::state::create(&path).unwrap());
        crate::board::state::create(&path)
    })
    .unwrap();
    lock.lock_shared().unwrap();
}

#[test]
fn selection_retries_when_an_update_prunes_before_the_usage_pin() {
    let f = Fixture::new();
    let old = f.install("1.0.0");
    let mut calls = 0;
    let (selected, _pin) = launch::select_pin_with(&f.layout, |runtime| {
        calls += 1;
        if calls == 1 {
            f.install("1.0.1");
            let _update = UpdateLock::acquire(&f.layout).unwrap();
            remove_unused(&f.layout, Ok(evidence()));
            assert!(!old.path.exists());
        }
        launch::pin(runtime)
    })
    .unwrap();
    assert_eq!(selected.version, "1.0.1");
    assert_eq!(calls, 2);
}

#[test]
fn replacement_preserves_the_actual_file_changed_after_the_last_comparison() {
    let f = Fixture::new();
    let path = f.path.join("client.json");
    crate::board::state::atomic_create(&path, b"old content").unwrap();
    let error = config_file::write_config_with(&path, b"old content", b"replacement", || {
        fs::write(&path, b"concurrent writer content").unwrap();
    })
    .unwrap_err();
    assert!(
        error.contains("actual displaced file was preserved"),
        "{error}"
    );
    let recovery = fs::read_dir(&f.path)
        .unwrap()
        .filter_map(|e| e.ok())
        .find(|e| {
            e.file_name()
                .to_string_lossy()
                .starts_with(".client-native-backup-")
        })
        .unwrap()
        .path();
    assert_eq!(
        fs::read(recovery.join("previous")).unwrap(),
        b"concurrent writer content"
    );
    assert_eq!(fs::read(&path).unwrap(), b"replacement");
}

#[cfg(unix)]
#[test]
fn an_inaccessible_tunnel_parent_is_unknown_evidence() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let parent = f.path.join("inaccessible");
    fs::create_dir(&parent).unwrap();
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o000)).unwrap();
    let observed = prune::tunnel_runtimes(&f.layout, &parent.join("tunnel"), &[]);
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(
        observed.is_err(),
        "unreadable tunnel root was treated as absent"
    );
}

#[cfg(unix)]
#[test]
fn redirected_tunnel_roots_and_missing_children_are_unknown_evidence() {
    let f = Fixture::new();
    let real = f.path.join("real");
    fs::create_dir(&real).unwrap();
    let redirected = f.path.join("redirected");
    std::os::unix::fs::symlink(&real, &redirected).unwrap();
    for root in [&redirected, &redirected.join("absent")] {
        assert!(prune::tunnel_runtimes(&f.layout, root, &[]).is_err());
    }
}

#[test]
fn release_base_defaults_to_the_github_release_and_honours_an_http_override() {
    use super::install::release_base;
    assert_eq!(
        release_base(None, "1.2.3").unwrap(),
        "https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/download/v1.2.3"
    );
    assert_eq!(
        release_base(Some("  "), "1.2.3").unwrap(),
        release_base(None, "1.2.3").unwrap()
    );
    assert_eq!(
        release_base(Some("http://127.0.0.1:8000/assets/"), "1.2.3").unwrap(),
        "http://127.0.0.1:8000/assets"
    );
    assert_eq!(
        release_base(Some("https://mirror.example.com/v1.2.3"), "1.2.3").unwrap(),
        "https://mirror.example.com/v1.2.3"
    );
    assert_eq!(
        release_base(Some("HTTPS://Mirror.Example.com/dir"), "1.2.3").unwrap(),
        "https://mirror.example.com/dir"
    );
    for refused in [
        "/srv/assets",
        "file:///srv/assets",
        "ftp://example.com/x",
        "http://",
        "https://host/dir?sig=abc",
        "https://host/dir#part",
    ] {
        let error = release_base(Some(refused), "1.2.3").unwrap_err();
        assert!(error.contains("PSEUDOLIFE_NATIVE_ARCHIVE_URL"), "{error}");
        assert!(error.contains("nothing was downloaded"), "{error}");
    }
}

#[test]
fn download_errors_name_the_archive_url_override_only_when_it_is_set() {
    use super::install::override_note;
    let detail = "native release download returned 404".to_owned();
    assert_eq!(
        override_note(detail.clone(), "https://github.com/x", false),
        detail
    );
    let noted = override_note(detail, "http://127.0.0.1:8000", true);
    assert!(noted.starts_with("native release download returned 404 (from PSEUDOLIFE_NATIVE_ARCHIVE_URL=http://127.0.0.1:8000;"), "{noted}");
    assert!(
        noted.contains("unset it to use the GitHub release"),
        "{noted}"
    );
}
