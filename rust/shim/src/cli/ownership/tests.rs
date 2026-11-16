use super::*;
use serde_json::json;

fn container() -> Value {
    json!({
        "Image": "sha256:daemon",
        "Config": {"User": "", "Env": ["PSEUDOLIFE_DAEMON_MODELS_DIR=/models"]},
        "Mounts": [
            {"Type": "volume", "Name": "p_state", "Destination": "/data", "RW": true},
            {"Type": "bind", "Source": "/srv/conf,1.yaml", "Destination": "/data/config.yaml", "RW": false},
            {"Type": "volume", "Name": "p_models", "Destination": "/models", "RW": true},
            {"Type": "tmpfs", "Destination": "/data/tmp", "RW": true},
            {"Type": "bind", "Source": "/etc/ssl", "Destination": "/etc/ssl", "RW": false},
        ],
    })
}

/// A helper answer as the script writes it: NUL-separated fields, hex.
fn answer(mode: &str, records: &[&[&[u8]]], end: bool) -> String {
    let mut bytes: Vec<u8> = Vec::new();
    for field in [mode.as_bytes(), b"1"] {
        bytes.extend_from_slice(field);
        bytes.push(0);
    }
    for record in records {
        for field in record.iter() {
            bytes.extend_from_slice(field);
            bytes.push(0);
        }
    }
    if end {
        bytes.extend_from_slice(b"end\0");
    }
    // As od -An -v -tx1 prints it.
    bytes
        .chunks(16)
        .map(|line| line.iter().map(|b| format!(" {b:02x}")).collect::<String>() + "\n")
        .collect()
}

fn usable() -> String {
    answer("probe-access", &[], true)
}

/// A Docker that answers each helper from a queue and records the calls;
/// `rm -f` is recorded but answered without the queue.
struct Fake {
    answers: Vec<(i32, String)>,
    calls: Vec<Vec<String>>,
    /// `docker info --format {{json .SecurityOptions}}`.
    info: (i32, String),
}

impl Docker for Fake {
    fn docker(&mut self, args: &[String]) -> (i32, String) {
        self.calls.push(args.to_vec());
        if args[0] == "rm" {
            return (0, String::new());
        }
        if args[0] == "info" {
            return self.info.clone();
        }
        self.answers.remove(0)
    }
}

fn fake(answers: Vec<(i32, String)>) -> Fake {
    Fake {
        answers,
        calls: Vec::new(),
        info: (
            0,
            r#"["name=seccomp,profile=builtin","name=cgroupns"]"#.into(),
        ),
    }
}

fn mode(call: &[String]) -> &str {
    if call[0] == "rm" || call[0] == "info" {
        return call[0].as_str();
    }
    call.iter()
        .find(|a| {
            a.starts_with("--probe-access")
                || a.ends_with("-ownership")
                || a.starts_with("--list-root")
        })
        .map(String::as_str)
        .unwrap_or("")
}

fn modes(docker: &Fake) -> Vec<&str> {
    docker.calls.iter().map(|c| mode(c)).collect()
}

/// A private scratch directory, removed when dropped.
struct Scratch(PathBuf);

impl Scratch {
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn scratch() -> Scratch {
    let dir = std::env::temp_dir().join(format!("pl-ownership-{}", uuid::Uuid::new_v4().simple()));
    fs::create_dir_all(&dir).unwrap();
    Scratch(dir)
}

fn layout_of(container: &Value) -> Option<Layout> {
    layout(container).unwrap()
}

/// The fixture with a user's writable bind mount below /data.
fn with_nested() -> Value {
    let mut daemon = container();
    daemon["Mounts"].as_array_mut().unwrap().push(
        json!({"Type": "bind", "Source": "/srv/exports", "Destination": "/data/extra", "RW": true}),
    );
    daemon
}

/// The one undo list a test's repair wrote.
fn only_log(dir: &Path) -> PathBuf {
    let logs: Vec<PathBuf> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(logs.len(), 1, "{logs:?}");
    assert!(
        logs[0]
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("ownership-repair-s-")
    );
    logs[0].clone()
}

#[test]
fn the_embedded_script_is_the_image_entrypoint_byte_for_byte() {
    let ops = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../ops/provision-daemon-models.sh");
    let committed = fs::read(&ops).expect("the entrypoint script");
    assert_eq!(SCRIPT.as_bytes(), committed.as_slice());
    assert!(
        !SCRIPT.contains('\r'),
        "the script must be LF-only for /bin/sh"
    );
}

#[test]
fn the_layout_keeps_only_the_state_and_model_mounts_and_walks_only_writable_ones() {
    let found = layout(&container()).unwrap().unwrap();
    assert_eq!(found.image, "sha256:daemon");
    let destinations: Vec<&str> = found
        .mounts
        .iter()
        .map(|m| m.destination.as_str())
        .collect();
    assert_eq!(
        destinations,
        ["/data", "/data/config.yaml", "/models", "/data/tmp"]
    );
    assert_eq!(
        found.roots,
        [
            ("/data".into(), Part::State),
            ("/models".into(), Part::Models)
        ]
    );
    // No state mount at /data: nothing to probe.
    let mut stateless = container();
    stateless["Mounts"] =
        json!([{"Type": "volume", "Name": "m", "Destination": "/models", "RW": true}]);
    assert_eq!(layout_of(&stateless), None);
    // An inspect object without typed mounts (or no object at all) too.
    assert_eq!(layout_of(&Value::Null), None);
    // A nested writable mount is probed, never a repair root.
    let nested = layout(&with_nested()).unwrap().unwrap();
    assert_eq!(
        nested.roots,
        [
            ("/data".into(), Part::State),
            ("/models".into(), Part::Models)
        ]
    );
    assert_eq!(nested.nested, [("/data/extra".into(), Part::State)]);
    // The models directory follows the container's own setting, slash or not.
    let mut moved = container();
    moved["Config"]["Env"] = json!(["PSEUDOLIFE_DAEMON_MODELS_DIR=/cache/"]);
    moved["Mounts"][2]["Destination"] = json!("/cache");
    let moved = layout(&moved).unwrap().unwrap();
    assert!(moved.roots.contains(&("/cache".into(), Part::Models)));
}

#[test]
fn tmpfs_mounts_stay_boundaries_and_volume_subpaths_are_kept() {
    let mut daemon = container();
    daemon["HostConfig"] = json!({
        "Tmpfs": {"/data/cache": "", "/elsewhere": ""},
        "Mounts": [{"Type": "volume", "Source": "p_models", "Target": "/models",
                    "VolumeOptions": {"Subpath": "rust"}}],
    });
    let found = layout(&daemon).unwrap().unwrap();
    let probe = probe_argv(&found, "n");
    assert!(
        probe.contains(&"type=tmpfs,dst=/data/cache".to_owned()),
        "{probe:?}"
    );
    assert!(
        probe.contains(&"type=tmpfs,dst=/data/tmp".to_owned()),
        "{probe:?}"
    );
    assert!(!probe.iter().any(|a| a.contains("/elsewhere")), "{probe:?}");
    assert!(
        probe.contains(&"type=volume,src=p_models,dst=/models,volume-subpath=rust".to_owned()),
        "{probe:?}"
    );
    // A tmpfs is never a root to walk.
    assert!(
        !found
            .roots
            .iter()
            .any(|(root, _)| root.contains("tmp") || root.contains("cache"))
    );
}

fn tail(mode: &str, mounts: &[&str], roots: &[&str]) -> Vec<String> {
    let mut tail: Vec<String> = [
        "--security-opt",
        "no-new-privileges",
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--no-healthcheck",
        "--pull",
        "never",
        "--pids-limit",
        "256",
    ]
    .map(String::from)
    .to_vec();
    for mount in mounts {
        tail.extend(["--mount".to_owned(), (*mount).to_owned()]);
    }
    tail.extend(["--entrypoint", "/bin/sh", "sha256:daemon", "-c"].map(String::from));
    tail.extend([
        SCRIPT.to_owned(),
        "provision-daemon-models".into(),
        mode.into(),
    ]);
    tail.extend(roots.iter().map(|r| (*r).to_owned()));
    tail
}

const STATE_MOUNTS: [&str; 3] = [
    "type=volume,src=p_state,dst=/data",
    "type=bind,\"source=/srv/conf,1.yaml\",dst=/data/config.yaml,readonly=true",
    "type=tmpfs,dst=/data/tmp",
];
const MODEL_MOUNTS: [&str; 1] = ["type=volume,src=p_models,dst=/models"];

#[test]
fn the_probe_runs_as_the_daemon_account_with_no_capabilities_network_or_writable_root() {
    let found = layout_of(&container()).unwrap();
    let mut expected: Vec<String> = [
        "run",
        "--rm",
        "--name",
        "n",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
    ]
    .map(String::from)
    .to_vec();
    let mounts = [&STATE_MOUNTS[..2], &MODEL_MOUNTS[..], &STATE_MOUNTS[2..]].concat();
    expected.extend(tail("--probe-access", &mounts, &["/data", "/models"]));
    assert_eq!(probe_argv(&found, "n"), expected);
}

#[test]
fn the_uid_0_helpers_get_only_their_capabilities_and_their_own_parts_mounts() {
    let found = layout_of(&container()).unwrap();
    let head = |caps: &[&str]| {
        let mut head: Vec<String> = [
            "run",
            "--rm",
            "--name",
            "n",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
        ]
        .map(String::from)
        .to_vec();
        for cap in caps {
            head.extend(["--cap-add".to_owned(), (*cap).to_owned()]);
        }
        head
    };
    // Spelled out: the ruled pair, not whatever the constant holds.
    assert_eq!(SEARCH_CAP, "DAC_OVERRIDE");
    let mut state = head(&["CHOWN", "DAC_OVERRIDE"]);
    state.extend(tail("--repair-ownership", &STATE_MOUNTS, &["/data"]));
    assert_eq!(repair_argv(&found, Part::State, "n"), state);
    let mut models = head(&["CHOWN", "DAC_OVERRIDE"]);
    models.extend(tail("--repair-ownership", &MODEL_MOUNTS, &["/models"]));
    assert_eq!(repair_argv(&found, Part::Models, "n"), models);
    // The listing that writes the undo list first cannot chown anything.
    let mut list = head(&["DAC_OVERRIDE"]);
    list.extend(tail("--list-root-owned", &STATE_MOUNTS, &["/data"]));
    assert_eq!(list_argv(&found, Part::State, "n"), list);
}

#[test]
fn the_probe_walks_nested_mounts_and_the_uid_0_helpers_get_them_read_only_and_unwalked() {
    // test_container.py's real-engine case mirrors exactly these roots.
    let found = layout_of(&with_nested()).unwrap();
    let probe = probe_argv(&found, "n");
    assert_eq!(
        probe[probe.len() - 3..],
        ["/data", "/models", "/data/extra"]
    );
    assert!(probe.contains(&"type=bind,source=/srv/exports,dst=/data/extra".to_owned()));
    for argv in [
        list_argv(&found, Part::State, "n"),
        repair_argv(&found, Part::State, "n"),
    ] {
        assert_eq!(argv.last().unwrap(), "/data", "{argv:?}");
        assert!(
            argv.contains(
                &"type=bind,source=/srv/exports,dst=/data/extra,readonly=true".to_owned()
            ),
            "{argv:?}"
        );
        assert!(
            argv.contains(&"type=volume,src=p_state,dst=/data".to_owned()),
            "{argv:?}"
        );
    }
}

#[test]
fn an_unusable_nested_mount_is_refused_with_its_source_before_anything_changes() {
    let unusable = answer(
        "probe-access",
        &[&[b"U", b"0", b"/data/extra/x"], &[b"U", b"0", b"/data/old"]],
        true,
    );
    let mut docker = fake(vec![(0, unusable)]);
    let dir = scratch();
    let error = preflight(&mut docker, &with_nested(), dir.path(), "s", |_| Ok(())).unwrap_err();
    assert_eq!(modes(&docker), ["--probe-access"]);
    assert!(
        error.contains("/data/extra/x") && error.contains("/srv/exports"),
        "{error}"
    );
    assert!(
        error.contains("change the owner of /srv/exports on the host"),
        "{error}"
    );
    assert!(error.contains("nothing was changed"), "{error}");
}

#[test]
fn a_root_the_account_owns_but_cannot_read_is_refused_never_passed() {
    let blind = answer("probe-access", &[&[b"R", b"/data"]], true);
    let mut docker = fake(vec![(0, blind)]);
    let dir = scratch();
    let error = preflight(&mut docker, &container(), dir.path(), "s", |_| Ok(())).unwrap_err();
    assert!(
        error.contains("owns /data but cannot read or enter it"),
        "{error}"
    );
    assert!(error.contains("SELinux"), "{error}");
    assert_eq!(modes(&docker), ["--probe-access"]);
}

#[test]
fn a_bind_root_on_a_remapped_or_unknown_engine_is_never_repaired() {
    let mut daemon = container();
    daemon["Mounts"][0] =
        json!({"Type": "bind", "Source": "/srv/state", "Destination": "/data", "RW": true});
    let found = layout_of(&daemon).unwrap();
    for info in [
        (0, r#"["name=seccomp,profile=builtin","name=rootless"]"#),
        (0, r#"["name=userns"]"#),
        (1, "Cannot connect to the Docker daemon"),
    ] {
        let mut docker = fake(vec![]);
        docker.info = (info.0, info.1.into());
        let dir = scratch();
        let error = repair(&mut docker, &found, Part::State, dir.path(), "s").unwrap_err();
        assert_eq!(modes(&docker), ["info"], "{info:?}");
        assert!(
            error.contains("/srv/state") && error.contains("nothing was changed"),
            "{error}"
        );
    }
    // An ordinary engine repairs a bind root as before.
    let listed = answer("list-root-owned", &[], true);
    let mut docker = fake(vec![(0, listed), (0, usable())]);
    let dir = scratch();
    repair(&mut docker, &found, Part::State, dir.path(), "s").unwrap();
    assert_eq!(
        modes(&docker),
        ["info", "--list-root-owned", "--probe-access"]
    );
}

#[test]
fn only_a_complete_hex_answer_counts_and_records_are_exact() {
    let probe = parse_probe(&answer(
        "probe-access",
        &[
            &[b"U", b"0", b"/data/a b\r\nc"],
            &[b"O", b"/data/own"],
            &[b"U", b"999", b"/models/\xff"],
        ],
        true,
    ))
    .unwrap();
    assert_eq!(
        probe.unusable,
        [
            Unusable {
                owner: "0".into(),
                path: b"/data/a b\r\nc".to_vec()
            },
            Unusable {
                owner: "999".into(),
                path: b"/models/\xff".to_vec()
            },
        ]
    );
    assert_eq!(probe.owner_fixable, [b"/data/own".to_vec()]);
    assert_eq!(shown(&probe.unusable[0].path), "/data/a b\\x0d\\nc");
    assert_eq!(shown(&probe.unusable[1].path), "/models/\\xff");
    assert_eq!(parse_probe(&usable()), Some(Probe::default()));
    for broken in [
        String::new(),
        "SCHEMA_META_VERSION = 58\n".into(),
        "58\n".into(),
        "probe-access\u{0}1\u{0}end\u{0}".into(),
        answer("probe-access", &[&[b"U", b"0", b"/data/a"]], false),
        answer("probe-access", &[&[b"X", b"/data"]], true),
        answer("repair-ownership", &[], true),
        " 7".into(),
    ] {
        assert_eq!(parse_probe(&broken), None, "{broken:?}");
    }
    let (records, complete) = parse_records(
        &answer(
            "repair-ownership",
            &[&[b"C", b"0", b"/data/x"], &[b"F", b"4", b"/data/y"]],
            true,
        ),
        "repair-ownership",
    );
    assert!(complete);
    assert_eq!(
        records
            .iter()
            .map(|(t, v)| (*t, v.path.clone()))
            .collect::<Vec<_>>(),
        [(b'C', b"/data/x".to_vec()), (b'F', b"/data/y".to_vec())]
    );
    // A killed run still yields its whole records.
    let mut partial = answer("repair-ownership", &[&[b"C", b"0", b"/data/x"]], false);
    partial.push_str(" 43 00 30 00 2f 64");
    let (records, complete) = parse_records(&partial, "repair-ownership");
    assert!(!complete);
    assert_eq!(records.len(), 1);
}

#[test]
fn a_healthy_host_runs_one_probe_and_no_root_helper() {
    let mut docker = fake(vec![(0, usable())]);
    let dir = scratch();
    let checked = preflight(&mut docker, &container(), dir.path(), "s", |_| Ok(()))
        .unwrap()
        .unwrap();
    assert!(checked.state_usable());
    assert_eq!(checked.models, None);
    assert_eq!(modes(&docker), ["--probe-access"]);
    assert!(
        docker
            .calls
            .iter()
            .all(|c| !c.contains(&"0:0".to_owned()) && !c.contains(&"--cap-add".to_owned()))
    );
    assert!(
        fs::read_dir(dir.path()).unwrap().next().is_none(),
        "no log without a repair"
    );
}

#[test]
fn models_are_listed_logged_repaired_and_reprobed_and_the_state_is_left_to_the_caller() {
    let unusable = answer(
        "probe-access",
        &[&[b"U", b"0", b"/data/old"], &[b"U", b"0", b"/models/m"]],
        true,
    );
    let listed = answer(
        "list-root-owned",
        &[
            &[b"L", b"0", b"/models/m"],
            &[b"L", b"50", b"/models/m/w\tx"],
        ],
        true,
    );
    let repaired = answer(
        "repair-ownership",
        &[
            &[b"C", b"0", b"/models/m"],
            &[b"C", b"50", b"/models/m/w\tx"],
        ],
        true,
    );
    let after = answer("probe-access", &[&[b"U", b"0", b"/data/old"]], true);
    let mut docker = fake(vec![(0, unusable), (0, listed), (0, repaired), (0, after)]);
    let dir = scratch();
    let checked = preflight(&mut docker, &container(), dir.path(), "20261116-1", |_| {
        Ok(())
    })
    .unwrap()
    .unwrap();
    assert_eq!(
        modes(&docker),
        [
            "--probe-access",
            "--list-root-owned",
            "--repair-ownership",
            "--probe-access"
        ]
    );
    assert_eq!(
        docker.calls[2].last().unwrap(),
        "/models",
        "the models repair never walks /data"
    );
    assert!(
        !docker.calls[2].iter().any(|a| a.contains("p_state")),
        "nor mounts it"
    );
    assert_eq!(
        checked.state,
        [Unusable {
            owner: "0".into(),
            path: b"/data/old".to_vec()
        }]
    );
    let models = checked.models.clone().unwrap();
    assert_eq!(models.changed, 2);
    let log = models.log.clone().unwrap();
    let text = fs::read_to_string(&log).unwrap();
    assert!(
        text.ends_with("0:0\t/models/m\n0:50\t/models/m/w\\tx\n"),
        "{text}"
    );
    assert!(text.contains("# mount: volume p_models at /models"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&log).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    assert!(models.line().contains(&log.display().to_string()));
    assert!(models.line().contains("model volume"));
}

#[test]
fn the_undo_list_is_written_before_the_change_or_nothing_changes() {
    // An unwritable log directory (a file in its place): no chown helper runs.
    let listed = answer("list-root-owned", &[&[b"L", b"0", b"/data/a"]], true);
    let mut docker = fake(vec![(0, listed)]);
    let dir = scratch();
    let blocked = dir.path().join("not-a-dir");
    fs::write(&blocked, "").unwrap();
    let found = layout_of(&container()).unwrap();
    let error = repair(&mut docker, &found, Part::State, &blocked, "s").unwrap_err();
    assert_eq!(modes(&docker), ["--list-root-owned"]);
    assert!(error.contains("so nothing was changed"), "{error}");
}

#[test]
fn a_repair_that_did_not_finish_keeps_the_full_list_and_removes_its_helper() {
    let listed = answer(
        "list-root-owned",
        &[&[b"L", b"0", b"/data/a"], &[b"L", b"0", b"/data/b"]],
        true,
    );
    // Interrupted: the client's capture lost the output entirely.
    let mut docker = fake(vec![(0, listed), (130, "command interrupted".into())]);
    let dir = scratch();
    let found = layout_of(&container()).unwrap();
    let error = repair(&mut docker, &found, Part::State, dir.path(), "s").unwrap_err();
    assert_eq!(
        modes(&docker),
        ["--list-root-owned", "--repair-ownership", "rm"]
    );
    let name = &docker.calls[1][docker.calls[1].iter().position(|a| a == "--name").unwrap() + 1];
    assert_eq!(docker.calls[2], ["rm", "-f", "-v", name.as_str()]);
    let log = only_log(dir.path());
    assert!(
        error.contains("did not finish") && error.contains(&log.display().to_string()),
        "{error}"
    );
    assert!(
        fs::read_to_string(log)
            .unwrap()
            .ends_with("0:0\t/data/a\n0:0\t/data/b\n")
    );
}

#[test]
fn a_second_failed_probe_refuses_with_the_path_and_the_one_fix_and_changes_nothing_more() {
    let unusable = answer("probe-access", &[&[b"U", b"999", b"/data/theirs"]], true);
    let nothing = answer("list-root-owned", &[], true);
    let mut docker = fake(vec![(0, nothing), (0, unusable)]);
    let dir = scratch();
    let found = layout_of(&container()).unwrap();
    let error = repair(&mut docker, &found, Part::State, dir.path(), "s").unwrap_err();
    // Nothing root-owned: no chown helper at all, only the probe again.
    assert_eq!(modes(&docker), ["--list-root-owned", "--probe-access"]);
    assert!(error.contains("/data/theirs"), "{error}");
    assert!(error.contains("uid 999 owns it"), "{error}");
    assert!(error.contains("Give it to 10001:10001"), "{error}");
    assert!(error.contains("nothing else was changed"), "{error}");
}

#[test]
fn a_refusing_caller_stops_before_any_repair() {
    let unusable = answer(
        "probe-access",
        &[&[b"U", b"0", b"/data/old"], &[b"U", b"0", b"/models/m"]],
        true,
    );
    let mut docker = fake(vec![(0, unusable)]);
    let dir = scratch();
    let error = preflight(&mut docker, &container(), dir.path(), "s", |state| {
        assert_eq!(state.len(), 1);
        Err("refused".into())
    })
    .unwrap_err();
    assert_eq!(error, "refused");
    assert_eq!(modes(&docker), ["--probe-access"]);
}

#[test]
fn a_probe_that_cannot_run_is_never_usable_and_says_why() {
    let found = layout_of(&container()).unwrap();
    let e71 = answer("probe-access", &[&[b"E", b"/data"]], false);
    for (code, out, cause) in [
        (
            125,
            "docker: Error response from daemon: invalid mount config",
            "invalid mount config",
        ),
        (127, "", "could not run /bin/sh"),
        (70, "", "mount table"),
        (71, "", "failed partway"),
        (71, e71.as_str(), "could not read everything below /data"),
        (0, "SCHEMA_META_VERSION = 58\n", "incomplete or unreadable"),
        (1, "", "exited 1"),
    ] {
        let mut docker = fake(vec![(code, out.into())]);
        let error = probe(&mut docker, &found).unwrap_err();
        assert!(error.contains(cause), "{code}: {error}");
        assert_eq!(
            modes(&docker).last().copied(),
            Some(if code == 0 { "--probe-access" } else { "rm" })
        );
    }
}

#[test]
fn no_message_tells_anyone_to_run_something_as_root() {
    let found = layout_of(&container()).unwrap();
    let mut messages = vec![
        unfixable(
            &[&Unusable {
                owner: "0".into(),
                path: b"/data/x".to_vec(),
            }],
            Part::State,
            None,
        ),
        unfixable(
            &[&Unusable {
                owner: "7".into(),
                path: b"/models/y".to_vec(),
            }],
            Part::Models,
            None,
        ),
        unfixable(
            &[&Unusable {
                owner: "?".into(),
                path: b"/data/c".to_vec(),
            }],
            Part::State,
            None,
        ),
    ];
    for (code, out) in [(125, ""), (127, ""), (70, ""), (71, ""), (72, ""), (1, "")] {
        messages.push(probe(&mut fake(vec![(code, out.into())]), &found).unwrap_err());
    }
    messages.push(
        repair(
            &mut fake(vec![(1, String::new())]),
            &found,
            Part::State,
            Path::new("/nonexistent-dir"),
            "s",
        )
        .unwrap_err(),
    );
    for message in messages {
        let lower = message.to_lowercase();
        for advice in [
            "sudo",
            "as root",
            "--user 0",
            "user: 0",
            "user: root",
            "run as root",
        ] {
            assert!(!lower.contains(advice), "{advice:?} in {message}");
        }
    }
}

#[test]
fn the_configured_user_decides_whether_an_exec_can_read_root_owned_state() {
    for (user, root) in [
        ("", true),
        ("root", true),
        ("0", true),
        ("0:0", true),
        ("10001:10001", false),
        ("pseudolife", false),
    ] {
        assert_eq!(
            runs_as_root(&json!({"Config": {"User": user}})),
            root,
            "{user}"
        );
    }
    assert!(runs_as_root(&json!({})));
}
