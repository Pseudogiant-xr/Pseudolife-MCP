//! The cases of ops/preflight.py's tests (tests/test_installer_resources.py,
//! tests/test_installer_lite.py, tests/test_installer_clone_authority.py),
//! run against the native check with the same fake Docker those tests use.
use super::*;
use serde_json::json;
use std::cell::RefCell;

const BEGIN_LINE: &str = BEGIN;
const END_LINE: &str = END;

struct Fake {
    root: PathBuf,
    home: PathBuf,
    containers: Vec<Value>,
    volumes: Vec<String>,
    busy: BTreeSet<u16>,
    env: BTreeMap<String, String>,
    calls: RefCell<Vec<Vec<String>>>,
    warnings: RefCell<Vec<String>>,
    docker_fails: bool,
}

impl Fake {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("preflight-fake-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(root.join("ops")).unwrap();
        let home = root.join("home");
        std::fs::create_dir_all(&home).unwrap();
        Self {
            root,
            home,
            containers: vec![],
            volumes: vec![],
            busy: BTreeSet::new(),
            env: BTreeMap::new(),
            calls: RefCell::new(vec![]),
            warnings: RefCell::new(vec![]),
            docker_fails: false,
        }
    }
    fn env_file(&self) -> PathBuf {
        self.root.join("ops/.env")
    }
    fn write_env(&self, text: &str) {
        std::fs::write(self.env_file(), text).unwrap();
    }
    fn check(&self) -> Result<()> {
        check(self, &self.root, &self.env_file())
    }
    fn refused(&self) -> String {
        match self.check() {
            Err(Refusal(message)) => message,
            Ok(()) => panic!("the check passed"),
        }
    }
    /// The fake Compose resolution of the resources fixture: single quotes
    /// are literal, anything else has its double quotes stripped and $VAR /
    /// ${VAR} substituted from the environment (unknown names stay).
    fn compose(&self, source: &Path) -> String {
        let text = std::fs::read_to_string(source).unwrap_or_default();
        let text = text.trim_start_matches('\u{feff}');
        let mut resolved: Vec<(String, String)> = vec![];
        for line in text.lines() {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            if !FIELDS.contains(&key) {
                continue;
            }
            let value = if let Some(rest) = value.strip_prefix('\'') {
                rest.trim_end_matches('\'')
                    .trim_start_matches('\'')
                    .to_owned()
            } else {
                self.substitute(value.trim_matches('"'))
            };
            resolved.retain(|(k, _)| k != key);
            resolved.push((key.to_owned(), value));
        }
        resolved
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join("\n")
    }
    fn substitute(&self, value: &str) -> String {
        let mut out = String::new();
        let mut rest = value;
        while let Some(at) = rest.find('$') {
            out.push_str(&rest[..at]);
            let after = &rest[at + 1..];
            if let Some(braced) = after.strip_prefix('{')
                && let Some(end) = braced.find('}')
            {
                let name = &braced[..end];
                match self.env.get(name) {
                    Some(v) => out.push_str(v),
                    None => out.push_str(&format!("${{{name}}}")),
                }
                rest = &braced[end + 1..];
                continue;
            }
            let end = after
                .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
                .unwrap_or(after.len());
            let name = &after[..end];
            if name.is_empty() {
                out.push('$');
            } else {
                match self.env.get(name) {
                    Some(v) => out.push_str(v),
                    None => out.push_str(&format!("${name}")),
                }
            }
            rest = &after[end..];
        }
        out.push_str(rest);
        out
    }
}

impl Drop for Fake {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

impl Host for Fake {
    fn docker(&self, args: &[&str], input: Option<&str>, failure: Option<&str>) -> Result<String> {
        self.calls
            .borrow_mut()
            .push(args.iter().map(|a| (*a).to_owned()).collect());
        if self.docker_fails {
            return refuse(failure.unwrap_or(DOCKER_UNSAFE));
        }
        Ok(match args {
            ["ps", "-a", ..] => self
                .containers
                .iter()
                .map(|c| c["Id"].as_str().unwrap().to_owned())
                .collect::<Vec<_>>()
                .join("\n"),
            ["inspect", ..] => serde_json::to_string(&self.containers).unwrap(),
            ["volume", "ls", ..] => self.volumes.join("\n"),
            ["compose", ..] => {
                let at = args.iter().position(|a| *a == "--env-file").unwrap();
                self.compose(Path::new(args[at + 1]))
            }
            ["exec", "-i", id, ..] => {
                let c = self.containers.iter().find(|c| c["Id"] == *id).unwrap();
                let initial = c["Config"]["Env"][0]
                    .as_str()
                    .unwrap()
                    .split_once('=')
                    .unwrap()
                    .1
                    .to_owned();
                let actual = c["_fixture_actual_password"]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or(initial);
                if input != Some(actual.as_str()) {
                    return refuse(failure.unwrap_or(DOCKER_UNSAFE));
                }
                String::new()
            }
            other => panic!("unexpected Docker operation: {other:?}"),
        })
    }
    fn port_busy(&self, port: u16) -> bool {
        self.busy.contains(&port)
    }
    fn file_text(&self, path: &Path) -> Option<String> {
        RealHost.file_text(path)
    }
    fn is_file(&self, path: &Path) -> bool {
        path.is_file()
    }
    fn is_dir(&self, path: &Path) -> bool {
        path.is_dir()
    }
    fn same_path(&self, first: &Path, second: &Path) -> bool {
        RealHost.same_path(first, second)
    }
    fn var(&self, name: &str) -> Option<String> {
        self.env.get(name).cloned()
    }
    fn home(&self) -> PathBuf {
        self.home.clone()
    }
    fn warn(&self, line: &str) {
        self.warnings.borrow_mut().push(line.to_owned());
    }
}

/// The resources fixture's `container()`.
fn container(name: &str, project: &str, running: bool, volume: &str, password: &str) -> Value {
    json!({"Id": format!("fixture-{name}"), "Name": format!("/pseudolife-mcp-{name}"),
           "Config": {"Labels": {"com.docker.compose.project": project},
                      "Env": [format!("POSTGRES_PASSWORD={password}")]},
           "State": {"Running": running},
           "Mounts": [{"Type": "volume", "Name": volume}],
           "NetworkSettings": {"Networks": {"pseudolife-mcp_default": {"IPAddress": "10.0.0.2"}},
                               "Ports": {"8765/tcp": [{"HostPort": "8765"}]}}})
}
fn ours(name: &str) -> Value {
    container(
        name,
        "pseudolife-mcp",
        true,
        "pseudolife-mcp-bank",
        "fixture-password",
    )
}

// -- tests/test_installer_resources.py ----------------------------------------

#[test]
fn foreign_containers_including_stopped_are_refused_without_changes() {
    for name in ["daemon", "postgres", "extractor", "other"] {
        for project in ["user-stack", ""] {
            let mut fake = Fake::new();
            fake.write_env("# keep exactly\r\n");
            fake.containers.push(container(
                name,
                project,
                false,
                "pseudolife-mcp-bank",
                "fixture-password",
            ));
            assert!(
                fake.refused().contains("Use that stack's own compose file"),
                "{name} {project}"
            );
            assert_eq!(
                std::fs::read(fake.env_file()).unwrap(),
                b"# keep exactly\r\n"
            );
            assert!(
                fake.calls
                    .borrow()
                    .iter()
                    .all(|c| ["ps", "inspect", "volume", "compose"].contains(&c[0].as_str()))
            );
        }
    }
}

#[test]
fn unrecorded_volume_requires_original_env_even_if_password_would_match() {
    for volume in ["pseudolife-mcp-bank", "pseudolife-mcp-state"] {
        let mut fake = Fake::new();
        fake.volumes.push(volume.into());
        assert!(fake.refused().contains("Restore the original ops/.env"));
        assert!(!fake.env_file().exists());
    }
}

#[test]
fn busy_port_is_a_refusal_not_a_warning() {
    for port in [8765, 5433] {
        let mut fake = Fake::new();
        fake.busy.insert(port);
        assert!(fake.refused().contains(&format!(
            "Stop or reconfigure the service listening on {port}"
        )));
    }
}

#[test]
fn our_managed_install_and_its_own_bound_port_are_allowed() {
    let mut fake = Fake::new();
    fake.write_env(&format!(
        "{BEGIN_LINE}\n# extractor: sidecar\n{END_LINE}\nPOSTGRES_PASSWORD=fixture-password\n"
    ));
    fake.volumes
        .extend(["pseudolife-mcp-bank".into(), "pseudolife-mcp-state".into()]);
    fake.containers.push(ours("daemon"));
    fake.busy.insert(8765);
    fake.check().unwrap();
}

#[test]
fn our_container_name_does_not_excuse_an_unrelated_listener() {
    let mut fake = Fake::new();
    let mut c = ours("daemon");
    c["NetworkSettings"]["Ports"] = json!({});
    fake.containers.push(c);
    fake.busy.insert(8765);
    assert!(fake.refused().contains("listening on 8765"));
}

#[test]
fn only_an_ipv4_loopback_tcp_publication_can_own_the_requested_port() {
    for (protocol, address) in [("udp", "127.0.0.1"), ("tcp", "::1"), ("tcp", "10.0.0.2")] {
        let mut fake = Fake::new();
        let mut c = ours("daemon");
        c["NetworkSettings"]["Ports"] =
            json!({format!("8765/{protocol}"): [{"HostPort": "8765", "HostIp": address}]});
        fake.containers.push(c);
        fake.busy.insert(8765);
        assert!(
            fake.refused().contains("listening on 8765"),
            "{protocol} {address}"
        );
    }
}

#[test]
fn pre_rename_volumes_recorded_in_crlf_env_are_allowed() {
    let mut fake = Fake::new();
    let content = "PSEUDOLIFE_BANK_VOLUME=ops_pseudolife_pgdata\r\nPSEUDOLIFE_STATE_VOLUME=ops_pseudolife_data\r\n";
    fake.write_env(content);
    fake.volumes.extend([
        "ops_pseudolife_pgdata".into(),
        "ops_pseudolife_data".into(),
        "pseudolife-mcp-bank".into(),
    ]);
    fake.check().unwrap();
    assert_eq!(std::fs::read_to_string(fake.env_file()).unwrap(), content);
}

#[test]
fn a_pre_rename_bank_is_not_replaced_by_an_empty_default_bank() {
    let mut fake = Fake::new();
    fake.volumes.push("ops_pseudolife_pgdata".into());
    assert!(
        fake.refused()
            .contains("PSEUDOLIFE_BANK_VOLUME=ops_pseudolife_pgdata")
    );
}

#[test]
fn changed_postgres_password_refuses_before_writing_anything() {
    let mut fake = Fake::new();
    fake.write_env("POSTGRES_PASSWORD=different-fixture-password\n");
    fake.containers.push(ours("postgres"));
    let message = fake.refused();
    assert!(message.contains("Restore POSTGRES_PASSWORD"));
    assert!(!message.contains("fixture-password"));
}

#[test]
fn compose_resolved_password_allows_a_legitimate_rerun() {
    for (quoted, password) in [
        ("fixture$SAMPLE_PART", "fixtureresolved"),
        ("'fixture$$word'", "fixture$$word"),
    ] {
        let mut fake = Fake::new();
        fake.env.insert("SAMPLE_PART".into(), "resolved".into());
        fake.write_env(&format!("POSTGRES_PASSWORD={quoted}\n"));
        fake.containers.push(container(
            "postgres",
            "pseudolife-mcp",
            true,
            "pseudolife-mcp-bank",
            password,
        ));
        fake.check().unwrap();
    }
}

#[test]
fn literal_container_password_does_not_mask_compose_interpolation() {
    let mut fake = Fake::new();
    fake.env.insert("SAMPLE_PART".into(), "resolved".into());
    fake.write_env("POSTGRES_PASSWORD=fixture$SAMPLE_PART\n");
    fake.containers.push(container(
        "postgres",
        "pseudolife-mcp",
        true,
        "pseudolife-mcp-bank",
        "fixture$SAMPLE_PART",
    ));
    assert!(fake.refused().contains("Restore POSTGRES_PASSWORD"));
}

#[test]
fn valid_rotated_password_is_not_compared_to_container_initialization() {
    let mut fake = Fake::new();
    fake.write_env("POSTGRES_PASSWORD=rotated-fixture\n");
    let mut c = container(
        "postgres",
        "pseudolife-mcp",
        true,
        "pseudolife-mcp-bank",
        "old-fixture",
    );
    c["_fixture_actual_password"] = json!("rotated-fixture");
    fake.containers.push(c);
    fake.check().unwrap();
    let calls = fake.calls.borrow();
    let probe = calls.iter().find(|c| c[0] == "exec").unwrap();
    assert_eq!(probe.last().unwrap(), "10.0.0.2"); // never the initdb-trusted loopback
    assert!(!probe.join(" ").contains("rotated-fixture"));
}

#[test]
fn recorded_volume_attached_to_a_foreign_container_is_not_ours() {
    let mut fake = Fake::new();
    fake.write_env("PSEUDOLIFE_BANK_VOLUME=custom-bank\n");
    fake.volumes.push("custom-bank".into());
    let mut c = container(
        "daemon",
        "user-stack",
        true,
        "custom-bank",
        "fixture-password",
    );
    c["Name"] = json!("/user-bank");
    fake.containers.push(c);
    assert!(fake.refused().contains("attached to another stack"));
}

#[test]
fn docker_failure_is_closed_and_does_not_echo_docker_stderr() {
    let mut fake = Fake::new();
    fake.docker_fails = true;
    let message = fake.refused();
    assert!(message.contains("docker info"));
    assert!(!message.contains("fixture-secret"));
}

#[test]
fn a_host_lite_bank_outside_the_checked_places_is_left_alone() {
    // test_resource_fixture_is_independent_of_a_host_lite_bank: the check
    // only reads; a bank where the fake host does not look changes nothing.
    let fake = Fake::new();
    fake.check().unwrap();
    assert!(!fake.env_file().exists());
}

// -- tests/test_installer_lite.py ---------------------------------------------

fn lite_daemon(running: bool) -> Value {
    json!({"Id": "fixture-daemon", "Name": "/pseudolife-mcp-daemon",
           "Config": {"Labels": {"com.docker.compose.project": "pseudolife-mcp"}},
           "State": {"Running": running}, "Mounts": [],
           "NetworkSettings": {"Ports": {"8765/tcp": [{"HostPort": "8765"}]}}})
}

fn lite_fake() -> Fake {
    let mut fake = Fake::new();
    let home = fake.home.display().to_string();
    fake.env
        .insert("LOCALAPPDATA".into(), format!("{home}/AppData/Local"));
    fake.env
        .insert("XDG_DATA_HOME".into(), format!("{home}/.local/share"));
    fake
}

fn initialized_bank(dir: &Path) -> PathBuf {
    std::fs::create_dir_all(dir.join("embedded_pg")).unwrap();
    std::fs::write(dir.join("embedded_pg/PG_VERSION"), "18\n").unwrap();
    std::fs::write(dir.join("embedded_pg/postmaster.pid"), "999999\n").unwrap();
    std::fs::write(dir.join("config.yaml"), "# fixture bank\n").unwrap();
    dir.to_path_buf()
}

fn first_lite_dir(fake: &Fake) -> PathBuf {
    lite_data_dirs(fake)[0].clone()
}

#[test]
fn initialized_lite_bank_refuses_before_docker_takes_the_free_port() {
    let fake = lite_fake();
    let bank = initialized_bank(&fake.home.join(".pseudolife-mcp"));
    fake.write_env("# keep this environment\n");
    let message = fake.refused();
    assert!(message.contains("pseudolife-mcp backup --data-dir"));
    assert!(message.contains(&bank.display().to_string()));
    assert!(
        message.contains("restore")
            && message.contains("configuration.md#move-a-lite-bank-to-docker")
    );
    assert!(message.contains("Nothing changed"));
    assert_eq!(
        std::fs::read_to_string(fake.env_file()).unwrap(),
        "# keep this environment\n"
    );
    assert!(bank.join("embedded_pg/PG_VERSION").is_file());
}

#[test]
fn active_lite_bank_gets_migration_advice_before_generic_busy_port() {
    let mut fake = lite_fake();
    initialized_bank(&fake.home.join(".pseudolife-mcp"));
    fake.busy.insert(8765);
    assert!(fake.refused().contains("pseudolife-mcp backup --data-dir"));
}

#[test]
fn empty_or_cache_only_lite_directory_is_not_a_bank() {
    for contents in ["empty", "cache", "stale-pid", "marker-directory"] {
        let fake = lite_fake();
        let cache = first_lite_dir(&fake).join("embedded_pg/bin");
        std::fs::create_dir_all(&cache).unwrap();
        match contents {
            "cache" => std::fs::write(cache.join("postgres.exe"), b"fixture binary cache").unwrap(),
            "stale-pid" => {
                std::fs::write(cache.parent().unwrap().join("postmaster.pid"), "999999\n").unwrap()
            }
            "marker-directory" => {
                std::fs::create_dir_all(cache.parent().unwrap().join("PG_VERSION")).unwrap()
            }
            _ => {}
        }
        fake.check().unwrap_or_else(|e| panic!("{contents}: {e:?}"));
    }
}

#[test]
fn the_platform_default_and_the_home_location_are_guarded() {
    // test_platform_default_matches_embedded_runtime_and_is_guarded, on the
    // platform the test runs on, with and without the folder variables set.
    for overridden in [true, false] {
        let mut fake = lite_fake();
        if !overridden {
            fake.env.remove("LOCALAPPDATA");
            fake.env.remove("XDG_DATA_HOME");
        }
        let bank = initialized_bank(&first_lite_dir(&fake));
        let message = fake.refused();
        assert!(message.contains(&bank.display().to_string()), "{message}");
    }
}

#[test]
fn custom_data_dir_is_guarded_without_mutating_it() {
    let mut fake = lite_fake();
    let bank = initialized_bank(&fake.root.join("custom bank"));
    fake.env
        .insert("PSEUDOLIFE_MCP_DATA_DIR".into(), bank.display().to_string());
    let message = fake.refused();
    assert!(message.contains(&format!("\"{}\"", bank.display())));
    assert!(bank.join("config.yaml").is_file());
}

#[test]
fn custom_empty_directory_does_not_hide_default_bank() {
    let mut fake = lite_fake();
    let bank = initialized_bank(&first_lite_dir(&fake));
    fake.env.insert(
        "PSEUDOLIFE_MCP_DATA_DIR".into(),
        fake.root.join("empty-custom").display().to_string(),
    );
    assert!(fake.refused().contains(&bank.display().to_string()));
}

#[test]
fn own_configured_docker_reruns_with_stopped_lite_files() {
    for running in [true, false] {
        for host_ip in ["", "127.0.0.1", "0.0.0.0"] {
            let mut fake = lite_fake();
            initialized_bank(&first_lite_dir(&fake));
            fake.write_env(&format!("{BEGIN_LINE}\n{END_LINE}\n"));
            fake.volumes
                .extend(["pseudolife-mcp-bank".into(), "pseudolife-mcp-state".into()]);
            let mut daemon = lite_daemon(running);
            daemon["NetworkSettings"]["Ports"]["8765/tcp"][0]["HostIp"] = json!(host_ip);
            fake.containers.push(daemon);
            if running {
                fake.busy.insert(8765);
            }
            fake.check()
                .unwrap_or_else(|e| panic!("{running} {host_ip}: {e:?}"));
        }
    }
}

#[test]
fn own_stopped_docker_does_not_excuse_active_lite_port() {
    let mut fake = lite_fake();
    initialized_bank(&first_lite_dir(&fake));
    fake.write_env(&format!("{BEGIN_LINE}\n{END_LINE}\n"));
    fake.containers.push(lite_daemon(false));
    fake.busy.insert(8765);
    assert!(fake.refused().contains("backup --data-dir"));
}

#[test]
fn docker_publication_on_another_address_does_not_excuse_active_lite() {
    for host_ip in ["::1", "192.0.2.2"] {
        let mut fake = lite_fake();
        initialized_bank(&first_lite_dir(&fake));
        fake.write_env(&format!("{BEGIN_LINE}\n{END_LINE}\n"));
        let mut daemon = lite_daemon(true);
        daemon["NetworkSettings"]["Ports"]["8765/tcp"][0]["HostIp"] = json!(host_ip);
        fake.containers.push(daemon);
        fake.busy.insert(8765);
        assert!(fake.refused().contains("backup --data-dir"));
    }
}

#[test]
fn own_recorded_volumes_allow_rerun_after_compose_down_unless_lite_port_is_busy() {
    for active_lite in [true, false] {
        let mut fake = lite_fake();
        initialized_bank(&first_lite_dir(&fake));
        fake.write_env(&format!("{BEGIN_LINE}\n{END_LINE}\n"));
        fake.volumes
            .extend(["pseudolife-mcp-bank".into(), "pseudolife-mcp-state".into()]);
        if active_lite {
            fake.busy.insert(8765);
            assert!(fake.refused().contains("backup --data-dir"));
        } else {
            fake.check().unwrap();
        }
    }
}

#[test]
fn partial_docker_setup_is_not_a_completed_migration() {
    for missing in ["env", "daemon"] {
        let mut fake = lite_fake();
        initialized_bank(&first_lite_dir(&fake));
        if missing != "env" {
            fake.write_env(&format!("{BEGIN_LINE}\n{END_LINE}\n"));
        }
        if missing != "daemon" {
            fake.containers.push(lite_daemon(false));
        }
        assert!(fake.refused().contains("lite"), "{missing}");
    }
}

#[test]
fn unrelated_busy_port_without_a_bank_keeps_generic_fix() {
    let mut fake = lite_fake();
    fake.busy.insert(8765);
    assert!(
        fake.refused()
            .contains("Stop or reconfigure the service listening on 8765")
    );
}

#[test]
fn own_udp_publication_does_not_excuse_a_busy_tcp_lite_bank() {
    let mut fake = lite_fake();
    initialized_bank(&first_lite_dir(&fake));
    fake.write_env(&format!("{BEGIN_LINE}\n{END_LINE}\n"));
    let mut daemon = lite_daemon(true);
    daemon["NetworkSettings"]["Ports"] = json!({"8765/udp": [{"HostPort": "8765"}]});
    fake.containers.push(daemon);
    fake.busy.insert(8765);
    assert!(fake.refused().contains("backup --data-dir"));
    assert!(lite_refusal(&fake, &fake.containers, true, false).is_some());
}

#[test]
fn preserved_stopped_cluster_no_longer_blocks_first_docker_setup() {
    let fake = lite_fake();
    let bank = initialized_bank(&first_lite_dir(&fake));
    let preserved = bank.with_file_name(format!(
        "{}.lite-preserved",
        bank.file_name().unwrap().to_string_lossy()
    ));
    std::fs::rename(bank.join("embedded_pg"), &preserved).unwrap();
    fake.check().unwrap();
    assert!(preserved.join("PG_VERSION").is_file());
    assert!(!fake.env_file().exists());
}

#[test]
fn refusal_names_the_steps_that_clear_the_guard_without_deleting_the_bank() {
    let fake = lite_fake();
    initialized_bank(&first_lite_dir(&fake));
    let message = fake.refused();
    assert!(message.contains("--out"));
    assert!(message.to_lowercase().contains("stop"));
    assert!(message.contains("move only embedded_pg"));
}

// -- tests/test_installer_clone_authority.py ---------------------------------

fn authority_daemon(working_dir: Option<&Path>, token: &str, tokens: &str) -> Value {
    let mut d = ours("daemon");
    d["Config"]["Env"] = json!([
        format!("PSEUDOLIFE_MCP_TOKEN={token}"),
        format!("PSEUDOLIFE_MCP_TOKENS={tokens}")
    ]);
    if let Some(dir) = working_dir {
        d["Config"]["Labels"]["com.docker.compose.project.working_dir"] =
            json!(dir.display().to_string());
    }
    d
}

fn saved(fake: &Fake, token: &str, tokens: &str) {
    fake.write_env(&format!(
        "{BEGIN_LINE}\n# extractor: sidecar\n{END_LINE}\nPSEUDOLIFE_MCP_TOKEN={token}\nPSEUDOLIFE_MCP_TOKENS='{tokens}'\n"
    ));
}

#[test]
fn a_second_clone_is_refused_before_credentials_or_config_are_written() {
    let mut fake = Fake::new();
    let original = fake.root.join("original/ops");
    std::fs::create_dir_all(&original).unwrap();
    saved(&fake, "fixture-original-token", "");
    fake.containers.push(authority_daemon(
        Some(&original),
        "fixture-original-token",
        "",
    ));
    assert!(fake.refused().contains("different install directory"));
}

#[test]
fn a_missing_recorded_checkout_names_recovery_without_minting() {
    let mut fake = Fake::new();
    saved(&fake, "fixture-original-token", "");
    let removed = fake.root.join("removed-clone/ops");
    fake.containers.push(authority_daemon(
        Some(&removed),
        "fixture-original-token",
        "",
    ));
    let message = fake.refused();
    assert!(message.contains("recorded install directory") && message.contains("missing"));
}

#[test]
fn a_local_env_cannot_lose_the_existing_bearer_and_mint_another() {
    let mut fake = Fake::new();
    saved(&fake, "", "");
    let ops = fake.root.join("ops");
    fake.containers
        .push(authority_daemon(Some(&ops), "fixture-original-token", ""));
    let message = fake.refused();
    assert!(message.contains("existing daemon authentication"));
    assert!(!message.contains("fixture-original-token"));
}

#[test]
fn unknown_origin_cannot_replace_an_existing_bearer() {
    let mut fake = Fake::new();
    saved(&fake, "fixture-different-token", "");
    fake.containers
        .push(authority_daemon(None, "fixture-original-token", ""));
    assert!(fake.refused().contains("existing daemon authentication"));
}

#[test]
fn explicit_nonempty_auth_change_in_original_env_is_allowed() {
    let mut fake = Fake::new();
    saved(&fake, "fixture-replacement-token", "");
    let ops = fake.root.join("ops");
    fake.containers
        .push(authority_daemon(Some(&ops), "fixture-original-token", ""));
    fake.check().unwrap();
}

#[test]
fn our_original_checkout_with_matching_credentials_is_allowed() {
    let mut fake = Fake::new();
    saved(&fake, "fixture-original-token", "");
    let ops = fake.root.join("ops");
    fake.containers
        .push(authority_daemon(Some(&ops), "fixture-original-token", ""));
    fake.check().unwrap();
}

#[test]
fn valid_multi_principal_entries_are_allowed_on_rerun() {
    let mut fake = Fake::new();
    saved(&fake, "", "fixture-a:client-a,fixture-b:client-b");
    let ops = fake.root.join("ops");
    fake.containers.push(authority_daemon(
        Some(&ops),
        "",
        "fixture-a:client-a,fixture-b:client-b",
    ));
    fake.check().unwrap();
}

#[test]
fn equivalent_multi_principal_order_and_case_do_not_change_auth() {
    let mut fake = Fake::new();
    saved(&fake, "", "fixture-a:CLIENT-A, fixture-b:client-b");
    fake.containers.push(authority_daemon(
        None,
        "",
        "fixture-b:client-b,fixture-a:client-a",
    ));
    fake.check().unwrap();
}

#[test]
fn multiple_env_files_are_named_as_context_not_as_a_missing_file() {
    let mut fake = Fake::new();
    saved(&fake, "fixture-original-token", "");
    let extra = fake.root.join("extra.env");
    std::fs::write(&extra, "EXTRA_SETTING=fixture\n").unwrap();
    let ops = fake.root.join("ops");
    let mut value = authority_daemon(Some(&ops), "fixture-original-token", "");
    value["Config"]["Labels"]["com.docker.compose.project.environment_file"] =
        json!(format!("{},{}", fake.env_file().display(), extra.display()));
    fake.containers.push(value);
    let message = fake.refused();
    assert!(message.contains("multiple environment files"));
    assert!(!message.contains("missing"));
}

#[test]
fn packaged_compose_working_dir_keeps_the_original_env_authority() {
    let mut fake = Fake::new();
    let packaged = fake
        .root
        .join("runtime/site-packages/pseudolife_memory/compose");
    std::fs::create_dir_all(&packaged).unwrap();
    saved(&fake, "fixture-original-token", "");
    let mut value = authority_daemon(Some(&packaged), "fixture-original-token", "");
    value["Config"]["Labels"]["com.docker.compose.project.environment_file"] =
        json!(fake.env_file().display().to_string());
    fake.containers.push(value);
    fake.check().unwrap();
}

#[test]
fn missing_packaged_compose_path_warns_with_matching_original_env() {
    let mut fake = Fake::new();
    saved(&fake, "fixture-original-token", "");
    let pruned = fake.root.join("pruned-runtime/compose");
    let mut value = authority_daemon(Some(&pruned), "fixture-original-token", "");
    value["Config"]["Labels"]["com.docker.compose.project.environment_file"] =
        json!(fake.env_file().display().to_string());
    fake.containers.push(value);
    fake.check().unwrap();
    assert!(
        fake.warnings
            .borrow()
            .iter()
            .any(|w| w.contains("working directory is unavailable"))
    );
}

// -- the native pieces ---------------------------------------------------------

#[test]
fn recorded_values_unquote_and_drop_a_comment_as_python_did() {
    let fake = Fake::new();
    fake.write_env("\u{feff}POSTGRES_PASSWORD='a #b'\nPSEUDOLIFE_BANK_VOLUME=bank # note\nPSEUDOLIFE_STATE_VOLUME=\"\nOTHER=x\n");
    let (values, managed) = environment(&fake, &fake.env_file()).unwrap();
    assert_eq!(values.get("POSTGRES_PASSWORD").unwrap(), "a #b");
    assert_eq!(values.get("PSEUDOLIFE_BANK_VOLUME").unwrap(), "bank");
    assert_eq!(values.get("PSEUDOLIFE_STATE_VOLUME").unwrap(), "");
    assert!(!values.contains_key("OTHER"));
    assert!(!managed);
}

#[test]
fn an_env_file_that_is_not_utf8_refuses() {
    let fake = Fake::new();
    std::fs::write(fake.env_file(), b"POSTGRES_PASSWORD=\xff\n").unwrap();
    assert_eq!(fake.refused(), UNREADABLE);
}

#[test]
fn a_real_listener_makes_the_port_busy() {
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    assert!(RealHost.port_busy(port));
    drop(listener);
}
