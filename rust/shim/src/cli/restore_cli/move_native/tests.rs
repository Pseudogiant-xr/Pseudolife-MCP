//! The native move dry run against recording fakes of both hosts.
//!
//! The fake ssh runs the real target helper ([`target::answer`]) against a
//! second fake Docker, so one run exercises the source preflight, the
//! request and reply, and the target's checks. Every test that completes a
//! dry run ends with [`assert_read_only`]: each command either host ran is
//! on the allowlist of commands that change nothing.
use std::{
    collections::HashMap,
    ffi::OsString,
    path::{Path, PathBuf},
    time::Duration,
};

use serde_json::{Value, json};

use super::{
    super::docker::{Docker, Input, Output, Ran},
    argv::{self, Request},
    probe::{self, READ_ONLY},
    remote::{self, Remote},
    target::{self, FRAME, PROTOCOL, Place},
    *,
};

const SECRET: &str = "s3cret-password-never-shown";
const SOURCE_ID: &str = "source-bank-id";
const TARGET_ID: &str = "target-bank-id";

fn fp(id: &str) -> String {
    probe::fingerprint(id).unwrap()
}

/// One host's Docker.
#[derive(Clone)]
struct Host {
    calls: Vec<Vec<String>>,
    docs: HashMap<String, Value>,
    meta: String,
    pg_version: String,
    db_size: String,
    du: String,
    tables: String,
    counts: String,
    /// `docker version` fails with this stderr.
    engine: Option<&'static str>,
    /// The bank holds one `retrieval_events` table, with its daemon's
    /// warm-up row in it.
    warmup_row: bool,
}

fn daemon_doc(running: bool, project: &str, dir: &str) -> Value {
    json!({
        "Image": "sha256:0123456789abcdef",
        "State": {"Running": running},
        "Config": {
            "Env": [format!("PSEUDOLIFE_MCP_DATABASE_URL=postgresql://pseudolife:{SECRET}@pseudolife-pg:5432/pseudolife_memory"), "PSEUDOLIFE_MCP_TOKEN=tok-secret", "PSEUDOLIFE_MCP_DATA_DIR=/data", "PATH=/bin"],
            "User": "pseudolife",
            "Image": "ghcr.io/pseudogiant-xr/pseudolife-daemon:0.25.0",
            "Labels": {"com.docker.compose.project": project, "com.docker.compose.project.working_dir": dir, "com.docker.compose.project.config_files": format!("{dir}/docker-compose.yml")}
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "PortBindings": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8765"}]}},
        "NetworkSettings": {"Networks": {"stack": {"IPAddress": "10.0.0.3"}}},
        "Mounts": [{"Type": "volume", "Destination": "/data", "Source": "/var/lib/docker/volumes/pseudolife-mcp-state/_data"}]
    })
}

fn pg_doc(project: &str) -> Value {
    json!({
        "Name": "/pseudolife-mcp-postgres",
        "State": {"Running": true},
        "Config": {"Labels": {"com.docker.compose.project": project}},
        "NetworkSettings": {"Networks": {"stack": {"Aliases": ["pseudolife-pg"], "IPAddress": "10.0.0.2"}}},
        "Mounts": [{"Type": "volume", "Destination": "/var/lib/postgresql", "Source": "/var/lib/docker/volumes/pseudolife-mcp-bank/_data"}]
    })
}

impl Host {
    fn new(bank_id: &str) -> Self {
        let mut docs = HashMap::new();
        docs.insert(
            "pseudolife-mcp-daemon".to_owned(),
            daemon_doc(true, "pseudolife-stack", "/home/u/pseudolife-stack"),
        );
        docs.insert(
            "pseudolife-mcp-postgres".to_owned(),
            pg_doc("pseudolife-stack"),
        );
        Self {
            calls: Vec::new(),
            docs,
            meta: format!("\"{bank_id}\""),
            pg_version: "180001".into(),
            db_size: "52428800".into(),
            du: "2048\t/data".into(),
            tables: r#"["entries","facts","meta"]"#.into(),
            counts: "0|0|0".into(),
            engine: None,
            warmup_row: false,
        }
    }
}

fn ok(stdout: &str) -> Ran {
    Ran {
        code: 0,
        stdout: stdout.as_bytes().to_vec(),
        ..Ran::default()
    }
}

impl Docker for Host {
    fn run_io(&mut self, args: &[String], _: Duration, _: Input<'_>, _: Output<'_>) -> Ran {
        self.calls.push(args.to_vec());
        let a: Vec<&str> = args.iter().map(String::as_str).collect();
        match a.as_slice() {
            ["inspect", name] => match self.docs.get(*name) {
                Some(doc) => ok(&json!([doc]).to_string()),
                None => Ran {
                    code: 1,
                    stderr: format!("Error: No such object: {name}").into_bytes(),
                    ..Ran::default()
                },
            },
            ["exec", _, "du", "-sk", "/data"] => ok(&self.du),
            ["version", ..] if self.engine.is_some() => Ran {
                code: 1,
                stderr: self.engine.unwrap_or_default().as_bytes().to_vec(),
                ..Ran::default()
            },
            ["version", ..] => ok("29.9.3"),
            _ => {
                let sql = a
                    .iter()
                    .position(|w| *w == "-c")
                    .and_then(|i| a.get(i + 1))
                    .copied()
                    .unwrap_or_default();
                // The count query names the meta keys it skips, so it is
                // matched first.
                if sql.starts_with("SELECT (SELECT count(*)") {
                    // A daemon's own warm-up row counts only when the query
                    // does not skip it.
                    let skips = sql.contains("\"retrieval_events\" WHERE NOT (query_text = 'warmup probe' AND session_id IS NULL AND episode_id IS NULL AND served = '[]'::jsonb)");
                    if self.warmup_row && !skips {
                        ok(&format!("{}|1", self.counts))
                    } else if self.warmup_row {
                        ok(&format!("{}|0", self.counts))
                    } else {
                        ok(&self.counts)
                    }
                } else if sql.contains("coordination_bank_id") {
                    ok(&self.meta)
                } else if sql.contains("server_version_num") {
                    ok(&self.pg_version)
                } else if sql.contains("pg_database_size") {
                    ok(&self.db_size)
                } else if sql.contains("json_agg(relname") {
                    ok(&self.tables)
                } else {
                    Ran {
                        code: 1,
                        stderr: b"unexpected".to_vec(),
                        ..Ran::default()
                    }
                }
            }
        }
    }
}

/// The target host the fake ssh reaches.
struct Far {
    docker: Host,
    health: Option<Value>,
    home: Option<PathBuf>,
    /// The nearest existing ancestor as given, its canonical form, and the
    /// free bytes there.
    nearest: Option<(PathBuf, PathBuf, Option<u64>)>,
    checkouts: Vec<PathBuf>,
    /// Paths whose links resolve elsewhere.
    links: HashMap<PathBuf, PathBuf>,
    timer: String,
    health_gets: Vec<String>,
}

impl Place for Far {
    type D = Host;
    fn docker(&mut self) -> Result<&mut Host, String> {
        Ok(&mut self.docker)
    }
    fn health(&mut self, url: &str) -> Option<Value> {
        self.health_gets.push(url.to_owned());
        self.health.clone()
    }
    fn home(&self) -> Option<PathBuf> {
        self.home.clone()
    }
    fn nearest(&self, path: &Path) -> Option<(PathBuf, PathBuf, Option<u64>)> {
        let (given, canonical, free) = self.nearest.clone()?;
        let rest = path.strip_prefix(&given).ok()?.to_path_buf();
        Some((canonical, rest, free))
    }
    fn canonical(&self, path: &Path) -> PathBuf {
        self.links
            .get(path)
            .cloned()
            .unwrap_or_else(|| path.to_path_buf())
    }
    fn in_checkout(&self, path: &Path) -> bool {
        self.checkouts.iter().any(|c| path.starts_with(c))
    }
    fn update_timer(&mut self) -> String {
        self.timer.clone()
    }
}

/// How the fake ssh answers.
enum Link {
    /// Run the helper against the target.
    Helper,
    /// ssh itself fails with this stderr (exit 255).
    Lost(&'static str),
    /// The remote command prints this and exits with this code.
    Prints(&'static str, i32),
    /// The remote command prints these on stdout and stderr, and exits so.
    PrintsBoth(&'static str, &'static str, i32),
}

struct Wire {
    far: Far,
    /// The exit ssh reports after the helper printed its reply.
    helper_exit: i32,
    link: Link,
    runs: Vec<(String, String, Vec<u8>)>,
}

impl Remote for Wire {
    fn run(&mut self, target: &str, command: &str, stdin: &[u8], _: Duration) -> remote::Ran {
        self.runs
            .push((target.to_owned(), command.to_owned(), stdin.to_vec()));
        match self.link {
            Link::Lost(stderr) => remote::Ran {
                code: 255,
                stderr: stderr.as_bytes().to_vec(),
                ..remote::Ran::default()
            },
            Link::Prints(stdout, code) => remote::Ran {
                code,
                stdout: stdout.as_bytes().to_vec(),
                ..remote::Ran::default()
            },
            Link::PrintsBoth(stdout, stderr, code) => remote::Ran {
                code,
                stdout: stdout.as_bytes().to_vec(),
                stderr: stderr.as_bytes().to_vec(),
                fault: None,
            },
            Link::Helper => {
                let request: protocol::Request = serde_json::from_slice(stdin).unwrap();
                let protocol::Op::Check { ask } = request.op else {
                    panic!("the preflight sends only check: {request:?}");
                };
                let answer = protocol::Answer {
                    protocol: protocol::PROTOCOL,
                    ok: true,
                    check: Some(target::answer(&mut self.far, &ask)),
                    ..protocol::Answer::default()
                };
                remote::Ran {
                    code: self.helper_exit,
                    stdout: format!(
                        "motd noise from a login script\n{FRAME}{}\n",
                        serde_json::to_string(&answer).unwrap()
                    )
                    .into_bytes(),
                    ..remote::Ran::default()
                }
            }
        }
    }
}

struct Both {
    here: Host,
    wire: Wire,
    local_health: Option<Value>,
    reached: Result<Value, String>,
    gets: Vec<String>,
}

impl World for Both {
    type D = Host;
    type R = Wire;
    fn docker(&mut self) -> Result<&mut Host, String> {
        Ok(&mut self.here)
    }
    fn remote(&mut self) -> Result<&mut Wire, String> {
        Ok(&mut self.wire)
    }
    fn local_health(&mut self, url: &str) -> Option<Value> {
        self.gets.push(url.to_owned());
        self.local_health.clone()
    }
    fn reach(&mut self, url: &str) -> Result<Value, String> {
        self.gets.push(url.to_owned());
        self.reached.clone()
    }
}

fn health(bank: &str, schema: i64) -> Value {
    json!({"status": "ok", "ready": true, "auth": true, "schema": schema, "bank": fp(bank), "version": "0.25.0"})
}

fn world() -> Both {
    Both {
        here: Host::new(SOURCE_ID),
        wire: Wire {
            far: Far {
                docker: Host::new(TARGET_ID),
                health: Some(health(TARGET_ID, 60)),
                home: Some(PathBuf::from("/home/u")),
                nearest: Some((
                    PathBuf::from("/home/u"),
                    PathBuf::from("/home/u"),
                    Some(100 << 30),
                )),
                checkouts: Vec::new(),
                links: HashMap::new(),
                timer: "enabled".into(),
                health_gets: Vec::new(),
            },
            link: Link::Helper,
            helper_exit: 0,
            runs: Vec::new(),
        },
        local_health: Some(health(SOURCE_ID, 60)),
        reached: Ok(health(TARGET_ID, 60)),
        gets: Vec::new(),
    }
}

fn request() -> Request {
    argv::parse(&words(&[
        "--to",
        "u@box",
        "--target-url",
        "https://box.example.com",
        "--no-keep-tokens",
        "--dry-run",
    ]))
    .unwrap()
}

fn words(items: &[&str]) -> Vec<OsString> {
    items.iter().map(OsString::from).collect()
}

/// Whether a docker argv is one that changes nothing: `inspect`, a
/// read-only psql `SELECT`/`SHOW`, or `du` of `/data`.
fn read_only(argv: &[String]) -> bool {
    let a: Vec<&str> = argv.iter().map(String::as_str).collect();
    match a.as_slice() {
        ["inspect", _] => true,
        ["version", "--format", "{{.Server.Version}}"] => true,
        ["exec", "pseudolife-mcp-daemon", "du", "-sk", "/data"] => true,
        [
            "exec",
            "-e",
            options,
            "pseudolife-mcp-postgres",
            "env",
            rest @ ..,
        ] if *options == READ_ONLY => {
            let Some(at) = rest.iter().position(|w| *w == "psql") else {
                return false;
            };
            let psql = &rest[at..];
            let Some(c) = psql.iter().position(|w| *w == "-c") else {
                return false;
            };
            let sql = psql.get(c + 1).copied().unwrap_or_default();
            psql.len() == c + 2
                && !sql.contains(';')
                && (sql.starts_with("SELECT ") || sql.starts_with("SHOW "))
        }
        _ => false,
    }
}

fn assert_read_only(world: &Both) {
    for (host, calls) in [
        ("source", &world.here.calls),
        ("target", &world.wire.far.docker.calls),
    ] {
        assert!(!calls.is_empty(), "{host} ran nothing");
        for call in calls {
            assert!(
                read_only(call),
                "{host} ran a command that may change state: {call:?}"
            );
        }
    }
    for (_, command, _) in &world.wire.runs {
        assert_eq!(
            command,
            &remote::sh(&script(protocol::PROTOCOL)),
            "the only remote command is the helper"
        );
    }
}

#[test]
fn a_dry_run_reads_both_hosts_changes_nothing_and_prints_the_plan() {
    let mut world = world();
    let plan = preflight(&mut world, &request()).expect("a plan");
    assert_read_only(&world);
    // The source asked the target about itself, by fingerprint and versions.
    let (to, _, body) = &world.wire.runs[0];
    assert_eq!(to, "u@box");
    let request: protocol::Request = serde_json::from_slice(body).unwrap();
    assert_eq!(request.protocol, protocol::PROTOCOL);
    assert!(body.ends_with(b"\n"), "the request is one line");
    let protocol::Op::Check { ask } = request.op else {
        panic!("the preflight sends check: {request:?}");
    };
    assert_eq!(ask.protocol, PROTOCOL);
    assert_eq!(ask.source_bank, fp(SOURCE_ID));
    assert_eq!((ask.source_schema, ask.source_pg_version), (60, 180001));
    // The files, plus the 8 GiB the restore keeps free beyond them.
    assert_eq!(ask.need_bytes, 52428800 + 2048 * 1024 + (8 << 30));
    // Both health reads and the target URL, by GET only.
    assert_eq!(
        world.gets,
        [
            "http://127.0.0.1:8765/health",
            "https://box.example.com/health"
        ]
    );
    assert_eq!(world.wire.far.health_gets, ["http://127.0.0.1:8765/health"]);
    assert_eq!(plan.target.work_dir, moves());
    let text = render(&plan, true);
    // Images by name, not id.
    assert!(
        text.contains("(image ghcr.io/pseudogiant-xr/pseudolife-daemon:0.25.0)"),
        "{text}"
    );
    assert!(!text.contains("sha256:0123456789abcdef"), "{text}");
    assert!(
        text.starts_with(
            "Move plan (dry run): both hosts checked; nothing was changed on either.\n"
        ),
        "{text}"
    );
    for step in [
        "pause the target's unattended update (enabled now)",
        "set the target daemon's restart policy to no for the move (it is unless-stopped)",
        "stop the source daemon pseudolife-mcp-daemon (docker stop -t 120; never removed)",
        "fence that database (ALLOW_CONNECTIONS false)",
        "write /data/moved.json into the stopped source: the commit point",
        "re-point this machine's clients to https://box.example.com",
    ] {
        assert!(text.contains(step), "{step}: {text}");
    }
    assert!(
        text.ends_with(
            "This run only checked both hosts: run it without --dry-run to move the bank.\n"
        ),
        "{text}"
    );
    // The move itself shows the same plan, saying it is about to run.
    let real = render(&plan, false);
    assert!(
        real.starts_with("Move plan: both hosts checked; nothing is changed until you confirm.\n"),
        "{real}"
    );
    assert!(
        real.contains("The move will, in this order:\n  1. claim the target for this move"),
        "{real}"
    );
    assert!(!real.contains("--dry-run"), "{real}");
    // No secret reaches the request, the reply, the plan or its document.
    let document = document(&plan).to_string();
    for shown in [
        document.as_str(),
        text.as_str(),
        &String::from_utf8_lossy(body),
    ] {
        assert!(
            !shown.contains(SECRET) && !shown.contains("tok-secret"),
            "{shown}"
        );
    }
    assert_eq!(document_of(&plan)["changed"], json!(false));
    assert_eq!(document_of(&plan)["target_url_tied"], json!(true));
}

/// The default work directory as the helper joins it on this platform
/// (a real target is POSIX).
fn moves() -> String {
    PathBuf::from("/home/u")
        .join(".pseudolife-mcp")
        .join("moves")
        .to_string_lossy()
        .into_owned()
}

/// One test case's change to a fake host.
type Change<T> = Box<dyn Fn(&mut T)>;

fn document_of(plan: &Plan) -> Value {
    document(plan)
}

#[test]
fn the_source_must_be_running_healthy_and_identified() {
    let cases: Vec<(Change<Both>, &str)> = vec![
        (
            Box::new(|w| {
                w.here.docs.insert(
                    "pseudolife-mcp-daemon".into(),
                    daemon_doc(false, "pseudolife-stack", "/x"),
                );
            }),
            "the source daemon pseudolife-mcp-daemon is not running",
        ),
        (
            Box::new(|w| {
                w.local_health = Some(
                    json!({"status": "ok", "ready": false, "bank": fp(SOURCE_ID), "schema": 60}),
                )
            }),
            "the source daemon is not healthy and ready",
        ),
        (
            Box::new(|w| {
                w.local_health = Some(json!({"status": "ok", "ready": true, "schema": 60}))
            }),
            "the source daemon reports no bank fingerprint yet",
        ),
        (
            Box::new(|w| w.here.meta = "\"another\"".into()),
            "the source daemon's fingerprint does not match the bank in pseudolife-mcp-postgres",
        ),
        (
            Box::new(|w| {
                w.here.docs.remove("pseudolife-mcp-postgres");
            }),
            "the source: the container pseudolife-mcp-postgres does not exist; start the Docker bank (docker compose up -d in its install directory), then retry",
        ),
    ];
    for (change, words) in cases {
        let mut world = world();
        change(&mut world);
        let why = preflight(&mut world, &request()).unwrap_err();
        assert!(why.contains(words), "{words}: {why}");
        assert!(
            !why.contains(SECRET) && !why.contains("tok-secret"),
            "{why}"
        );
        assert!(!why.contains("--container"), "{why}");
        assert!(world.wire.runs.is_empty(), "the target is not asked: {why}");
        for call in &world.here.calls {
            assert!(read_only(call), "{call:?}");
        }
    }
}

#[test]
fn the_target_must_be_an_installed_empty_healthy_newer_bank() {
    let cases: Vec<(Change<Far>, &str)> = vec![
        (
            Box::new(|f| f.docker.counts = "12|3|0".into()),
            "the target bank is not empty (entries 12, facts 3): move never merges into or overwrites a used bank",
        ),
        (
            Box::new(|f| f.docker.meta = format!("\"{SOURCE_ID}\"")),
            "the target daemon's fingerprint does not match the bank in pseudolife-mcp-postgres",
        ),
        (
            Box::new(|f| {
                f.health = Some(health(SOURCE_ID, 60));
                f.docker.meta = format!("\"{SOURCE_ID}\"");
            }),
            "the target is this same bank",
        ),
        (
            Box::new(|f| f.health = Some(health(TARGET_ID, 59))),
            "the target's schema (59) is older than the source's (60)",
        ),
        (
            Box::new(|f| f.docker.pg_version = "170004".into()),
            "the target's PostgreSQL (17) is an older major version than the source's (18)",
        ),
        (
            Box::new(|f| {
                f.docker
                    .docs
                    .insert("pseudolife-mcp-postgres".into(), pg_doc("other"));
            }),
            "are not one Docker Compose project",
        ),
        (
            Box::new(|f| f.checkouts.push(PathBuf::from("/home/u/pseudolife-stack"))),
            "runs from a source checkout (/home/u/pseudolife-stack)",
        ),
        (
            Box::new(|f| {
                f.docker.docs.insert(
                    "pseudolife-mcp-daemon".into(),
                    daemon_doc(false, "pseudolife-stack", "/home/u/pseudolife-stack"),
                );
            }),
            "the target daemon pseudolife-mcp-daemon is not running",
        ),
        (
            Box::new(|f| f.health = Some(json!({"status": "ok", "ready": true, "schema": 60}))),
            "the target daemon has not read its bank's identity yet (its /health shows no fingerprint while pseudolife-mcp-postgres holds one)",
        ),
        (
            Box::new(|f| f.docker.meta = String::new()),
            "the target daemon's fingerprint does not match the bank in pseudolife-mcp-postgres",
        ),
        (
            Box::new(|f| {
                // The home resolves into the bank's state volume.
                f.nearest = Some((
                    PathBuf::from("/home/u"),
                    PathBuf::from("/var/lib/docker/volumes/pseudolife-mcp-state/_data"),
                    Some(1 << 40),
                ))
            }),
            "is inside a volume of the target's bank",
        ),
        (
            Box::new(|f| {
                f.nearest = Some((
                    PathBuf::from("/home/u"),
                    PathBuf::from("/home/u"),
                    Some(1024),
                ))
            }),
            "the move's files may take up to 8244 MiB; free space there or name another --target-work-dir",
        ),
        (
            Box::new(|f| f.home = None),
            "the target's move work directory cannot be resolved",
        ),
    ];
    for (change, words) in cases {
        let mut world = world();
        change(&mut world.wire.far);
        let why = preflight(&mut world, &request()).unwrap_err();
        assert!(why.contains(words), "{words}: {why}");
        assert!(
            !why.contains(SECRET) && !why.contains("tok-secret"),
            "{why}"
        );
        assert_read_only(&world);
    }
}

#[test]
fn postgresql_is_compared_by_major_version() {
    // 18.0 on the target, 18.1 on the source: a dump restores.
    let mut world = world();
    world.wire.far.docker.pg_version = "180000".into();
    preflight(&mut world, &request()).expect("a minor release apart is fine");
}

#[test]
fn a_work_directory_inside_a_bank_volume_is_found_through_links() {
    // Docker reports the volume under /home, which is a link to /var/home;
    // the work directory resolves under /var/home.
    let mut world = world();
    let far = &mut world.wire.far;
    far.docker.docs.insert(
        "pseudolife-mcp-daemon".into(),
        {
            let mut doc = daemon_doc(true, "pseudolife-stack", "/home/u/pseudolife-stack");
            doc["Mounts"] = json!([{"Type": "volume", "Destination": "/data", "Source": "/home/u/.local/share/docker/volumes/state/_data"}]);
            doc
        },
    );
    far.links.insert(
        PathBuf::from("/home/u/.local/share/docker/volumes/state/_data"),
        PathBuf::from("/var/home/u/.local/share/docker/volumes/state/_data"),
    );
    far.nearest = Some((
        PathBuf::from("/home/u"),
        PathBuf::from("/var/home/u/.local/share/docker/volumes/state/_data"),
        Some(100 << 30),
    ));
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(
        why.contains("is inside a volume of the target's bank"),
        "{why}"
    );
}

#[test]
fn a_reply_printed_before_ssh_failed_is_still_the_answer() {
    let mut world = world();
    world.wire.helper_exit = 255;
    preflight(&mut world, &request()).expect("the framed reply is read first");
}

#[test]
fn a_local_network_refusal_is_not_read_as_a_login_refusal() {
    let mut first = world();
    first.wire.link = Link::Lost("ssh: connect to host box port 22: Permission denied\n");
    let why = preflight(&mut first, &request()).unwrap_err();
    assert!(why.starts_with("ssh could not reach u@box (ssh said: ssh: connect to host box port 22: Permission denied)"), "{why}");
    // A changed key: ssh's own removal line, not a guessed one.
    let mut world = world();
    world.wire.link = Link::Lost(
        "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\nOffending ECDSA key in /home/u/.ssh/known_hosts:3\n  remove with:\n  ssh-keygen -f '/home/u/.ssh/known_hosts' -R '[box]:2222'\nHost key verification failed.\n",
    );
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(
        why.contains(
            "then run ssh-keygen -f '/home/u/.ssh/known_hosts' -R '[box]:2222' and connect once"
        ),
        "{why}"
    );
}

#[test]
fn a_work_directory_below_a_symlinked_parent_keeps_its_own_name() {
    let mut world = world();
    // ~/move-link -> /srv/move-storage; new-moves is still to be made.
    world.wire.far.nearest = Some((
        PathBuf::from("/home/u/move-link"),
        PathBuf::from("/srv/move-storage"),
        Some(100 << 30),
    ));
    let mut asked = request();
    asked.target_work_dir = Some("~/move-link/new-moves".into());
    let plan = preflight(&mut world, &asked).expect("a plan");
    assert_eq!(
        plan.target.work_dir,
        PathBuf::from("/srv/move-storage")
            .join("new-moves")
            .to_string_lossy()
    );
}

#[test]
fn an_unmeasured_source_size_refuses_rather_than_counting_as_zero() {
    for broken in ["db", "data"] {
        let mut world = world();
        if broken == "db" {
            world.here.db_size = "not a number".into();
        } else {
            world.here.du = String::new();
        }
        let why = preflight(&mut world, &request()).unwrap_err();
        assert!(
            why.starts_with("cannot measure the source bank's size"),
            "{broken}: {why}"
        );
        assert!(
            world.wire.runs.is_empty(),
            "{broken}: the target is not asked"
        );
    }
}

#[test]
fn a_fresh_targets_own_warmup_search_is_not_user_data() {
    let mut fresh = world();
    fresh.wire.far.docker.tables = r#"["entries","facts","meta","retrieval_events"]"#.into();
    fresh.wire.far.docker.warmup_row = true;
    preflight(&mut fresh, &request()).expect("the warm-up row alone is a fresh bank");
    assert_read_only(&fresh);
    // A real search beside it still counts.
    let mut world = world();
    world.wire.far.docker.tables = r#"["entries","facts","meta","retrieval_events"]"#.into();
    world.wire.far.docker.counts = "0|0|0|2".into();
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(
        why.contains("the target bank is not empty (retrieval_events 2)"),
        "{why}"
    );
}

#[test]
fn docker_that_this_user_cannot_use_is_named_with_dockers_reason_on_either_host() {
    const DENIED: &str = "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: Get \"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/version\": dial unix /var/run/docker.sock: connect: permission denied\n";
    let mut first = world();
    first.here.engine = Some(DENIED);
    let why = preflight(&mut first, &request()).unwrap_err();
    assert!(why.starts_with("the source: Docker cannot be used here (permission denied while trying to connect to the Docker daemon socket"), "{why}");
    assert!(why.ends_with("run move as a user who can run docker (or set PSEUDOLIFE_DOCKER to a docker that works), then retry"), "{why}");
    let mut world = world();
    world.wire.far.docker.engine = Some(DENIED);
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(
        why.starts_with("the target's bank: Docker cannot be used here (permission denied"),
        "{why}"
    );
    assert_read_only(&world);
}

#[test]
fn ssh_failures_name_their_cause_and_never_touch_host_key_checking() {
    for (stderr, words) in [
        (
            "@@@@@@@@@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\nHost key verification failed.\n",
            "the ssh host key of u@box has changed since this machine recorded it (ssh refused: Host key verification failed.). A reinstalled host does this, and so does an attack: confirm the new key with whoever runs that host, then run ssh-keygen -R box and connect once with ssh u@box to record it; move never accepts a host key itself",
        ),
        (
            "No ED25519 host key is known for box and you have requested strict checking.\nHost key verification failed.\n",
            "the ssh host key of u@box is not known on this machine (ssh refused: Host key verification failed.). Connect once with ssh u@box, check the fingerprint it shows against the target's own, and accept it",
        ),
        (
            "u@box: Permission denied (publickey).\n",
            "ssh could not log in to u@box with a key (ssh said: u@box: Permission denied (publickey).)",
        ),
        (
            "ssh: Could not resolve hostname box: Name or service not known\n",
            "ssh could not reach u@box (ssh said: ssh: Could not resolve hostname box: Name or service not known)",
        ),
    ] {
        let mut world = world();
        world.wire.link = Link::Lost(stderr);
        let why = preflight(&mut world, &request()).unwrap_err();
        assert!(why.starts_with(words), "{words}\n{why}");
        assert_eq!(world.wire.runs.len(), 1, "one attempt, no retry");
    }
    // The options: non-interactive, and nothing about host keys.
    let argv = remote::argv("u@box", "true");
    assert!(
        argv.windows(2).any(|w| w == ["-o", "BatchMode=yes"]),
        "{argv:?}"
    );
    for word in &argv {
        let lower = word.to_ascii_lowercase();
        assert!(
            !lower.contains("hostkey")
                && !lower.contains("knownhosts")
                && !lower.contains("stricthost"),
            "{argv:?}"
        );
    }
    // The target cannot become an option.
    assert_eq!(&argv[argv.len() - 3..], ["--", "u@box", "true"]);
}

#[test]
fn a_missing_old_or_mismatched_target_client_is_named() {
    for (link, words) in [
        (
            Link::Prints("PSEUDOLIFE-MOVE-NO-CLIENT\n", 3),
            "u@box has no native pseudolife-mcp client (move looks for its stable launcher, ~/.local/bin and PATH); install it there with the installer",
        ),
        (
            // argparse writes its error to stderr, after the motd on stdout.
            Link::PrintsBoth(
                "motd noise from a login script\n",
                "usage: pseudolife-mcp [-h] ...\npseudolife-mcp: error: argument mode: invalid choice: 'move-target'\n",
                2,
            ),
            "the pseudolife-mcp on u@box has no native move helper (it answered: pseudolife-mcp: error: argument mode: invalid choice: 'move-target'); update it there with pseudolife-mcp update",
        ),
        (
            // A newer client, speaking a protocol this one does not.
            Link::Prints(
                "PSEUDOLIFE-MOVE-TARGET {\"protocol\":3,\"version\":\"0.30.0\",\"ok\":false,\"error\":\"\"}\n",
                3,
            ),
            "the client on u@box (0.30.0) speaks move protocol 3, and this one",
        ),
        (
            // An older client that speaks only protocol 1 refuses 2 itself.
            Link::Prints(
                "PSEUDOLIFE-MOVE-TARGET {\"protocol\":1,\"version\":\"0.23.0\",\"refusals\":[\"the target's pseudolife-mcp speaks move protocol 1, and the source asked for \\\"2\\\"; update the older client (pseudolife-mcp update) so both match, then retry\"],\"facts\":null,\"work_dir\":\"\",\"free_bytes\":null,\"update_timer\":\"\"}\n",
                3,
            ),
            "the target's pseudolife-mcp speaks move protocol 1, and the source asked for \"2\"; update the older client",
        ),
        (
            Link::Prints("PSEUDOLIFE-MOVE-TARGET {not json\n", 0),
            "the native move helper on u@box answered something this client cannot read",
        ),
    ] {
        let mut world = world();
        world.wire.link = link;
        let why = preflight(&mut world, &request()).unwrap_err();
        assert!(why.starts_with(words), "{words}\n{why}");
    }
}

#[test]
fn a_fresh_target_without_a_fingerprint_is_accepted_and_its_url_checked_by_what_it_answers() {
    let fresh = || {
        let mut world = world();
        world.wire.far.docker.meta = String::new();
        world.wire.far.health = Some(
            json!({"status": "ok", "ready": true, "auth": true, "schema": 60, "bank": null, "version": "0.25.0"}),
        );
        world.reached =
            Ok(json!({"status": "ok", "schema": 60, "bank": null, "version": "0.25.0"}));
        world
    };
    let mut world = fresh();
    let plan = preflight(&mut world, &request()).expect("a fresh target is accepted");
    let text = render(&plan, true);
    assert!(
        text.contains("an empty bank (no identity yet: a fresh install)"),
        "{text}"
    );
    // The URL is not tied to the target, and the plan says so.
    assert!(!plan.url_tied);
    assert!(
        text.contains("this URL is not tied to u@box: check it reaches that host"),
        "{text}"
    );
    assert_eq!(document(&plan)["target_url_tied"], json!(false));
    assert_eq!(plan.target.facts.unwrap().bank, None);
    assert_read_only(&world);
    // The URL reaches some other daemon: refused by what it answers.
    let mut world = fresh();
    world.reached = Ok(json!({"status": "ok", "schema": 58, "bank": null, "version": "0.21.0"}));
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(
        why.contains("that does not answer as u@box's does (version \"0.25.0\", schema Some(60))"),
        "{why}"
    );
}

#[test]
fn the_target_url_must_reach_the_targets_own_daemon() {
    let mut first = world();
    first.reached = Err("connection refused".into());
    let why = preflight(&mut first, &request()).unwrap_err();
    assert!(why.starts_with("--target-url https://box.example.com does not answer from this host (connection refused)"), "{why}");
    let mut world = world();
    world.reached = Ok(health(SOURCE_ID, 60));
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(why.contains("reaches a daemon whose bank"), "{why}");
    assert_read_only(&world);
}

#[test]
fn the_helper_answers_a_protocol_it_does_not_speak_with_3_never_255() {
    assert_eq!(target::run(&words(&["3"])), 3);
    assert_eq!(target::run(&words(&[])), 3);
    assert_eq!(target::run(&words(&["1", "extra"])), 3);
    assert_eq!(target::run(&words(&["2", "extra"])), 3);
}

#[test]
fn argv_refusals_come_before_any_effect_in_order() {
    let refused = |items: &[&str]| argv::refusal(&argv::parse(&words(items)).unwrap());
    let base = [
        "--to",
        "u@box",
        "--target-url",
        "http://h:8765",
        "--no-keep-tokens",
        "--dry-run",
    ];
    assert_eq!(refused(&base), None);
    let without = |drop: &str| -> Vec<&str> {
        let mut out = Vec::new();
        let mut skip = false;
        for (i, w) in base.iter().enumerate() {
            if skip {
                skip = false;
                continue;
            }
            if *w == drop {
                skip = drop == "--target-url" && i + 1 < base.len();
                continue;
            }
            out.push(*w);
        }
        out
    };
    let starts = |items: &[&str], words: &str| {
        let why = refused(items).unwrap_or_default();
        assert!(why.starts_with(words), "{items:?}: {why}");
        assert!(why.ends_with(". Nothing was changed"), "{why}");
    };
    // The move itself, and resuming one, pass the argv checks.
    assert_eq!(refused(&without("--dry-run")), None);
    assert_eq!(refused(&[&base[..], &["--resume"]].concat()), None);
    starts(
        &[&base[..], &["--target-checkout", "/srv/x"]].concat(),
        "--target-checkout is not supported",
    );
    starts(
        &[&base[..], &["--expose-target"]].concat(),
        "--expose-target is not available in the native client yet: expose the target's daemon yourself",
    );
    starts(
        &[&base[..], &["--target-env-file", "/x/.env"]].concat(),
        "--target-env-file and --target-compose-file are not available in the native client yet",
    );
    starts(&without("--target-url"), "--target-url is required");
    starts(
        &without("--no-keep-tokens"),
        "carrying the source's tokens to the target is not available",
    );
    starts(
        &[&base[..], &["--target-work-dir", "rel/dir"]].concat(),
        "--target-work-dir must be an absolute path",
    );
    starts(
        &[&base[..], &["--target-work-dir", "/tmp/a\nb"]].concat(),
        "--target-work-dir holds a control character",
    );
    starts(
        &[
            &base[..],
            &["--target-work-dir", "/srv/x/../pseudolife-mcp-bank/_data"],
        ]
        .concat(),
        "--target-work-dir must name the directory itself, without a .. component",
    );
    // A --to that is no ssh target outranks everything, as Python orders it.
    for to in ["", "-oProxyCommand=x", "a b", "a\u{1}b", "a\u{7f}b"] {
        let request = Request {
            to: to.to_owned(),
            dry_run: true,
            ..Request::default()
        };
        assert_eq!(
            argv::refusal(&request).as_deref(),
            Some("--to takes an ssh target such as root@box or a ~/.ssh/config alias"),
            "{to:?}"
        );
    }
    // A bad --target-url outranks a bad --to, in Python's words.
    assert_eq!(
        refused(&["--to", "a b", "--target-url", "http://h/x"]).as_deref(),
        Some(
            "--target-url must be an http(s) origin such as http://100.64.0.2:8765 (no path, query or credentials)"
        )
    );
    // Without --dry-run the checks are the same, in the same order.
    starts(
        &["--to", "u@box", "--target-checkout", "/x"],
        "--target-checkout is not supported",
    );
    // --target-release is the only mode now: accepted.
    assert_eq!(
        refused(&[&base[..], &["--target-release", "--yes", "--json"]].concat()),
        None
    );
}

#[test]
fn target_urls_are_origins() {
    for good in [
        "http://100.64.0.2:8765",
        "https://box.example.com/",
        "HTTP://Box:1",
        "http://[::1]:8765",
        "http://h:65535",
    ] {
        assert_eq!(argv::origin(good), Ok(()), "{good}");
    }
    for bad in [
        "ftp://h",
        "http://",
        "http://h/x",
        "http://h?q",
        "http://h#f",
        "http://u@h",
        "http://h:0",
        "http://h:65536",
        "http://h:",
        "http://[::1",
        "http://[zz]:1",
        "http://h x",
        "http://h_x",
    ] {
        assert!(argv::origin(bad).is_err(), "{bad}");
    }
}

#[test]
fn shapes_this_parser_does_not_take_keep_the_deferral() {
    for items in [
        &["--to=u@box", "--dry-run"][..],
        &["--dry", "--to", "u@box"],
        &["--yes", "--yes", "--to", "u@box"],
        &["--to", "u@box", "--bogus"],
        &["--to", "u@box", "extra"],
        &["--to", "u@box", "--target-url", "-x"],
        &["--dry-run"],
        &["--to", "u@box", "--", "x"],
    ] {
        assert_eq!(argv::parse(&words(items)), None, "{items:?}");
    }
    assert_eq!(super::run(&words(&["--to=u@box"])), None);
}

#[test]
fn a_host_network_daemon_is_reached_where_it_binds() {
    let doc = |env: &[&str]| json!({"HostConfig": {"NetworkMode": "host"}, "Config": {"Env": env}});
    assert_eq!(probe::host_network_url(&doc(&[])), "http://127.0.0.1:8765");
    assert_eq!(
        probe::host_network_url(&doc(&[
            "PSEUDOLIFE_MCP_PORT=18765",
            "PSEUDOLIFE_MCP_HOST=0.0.0.0"
        ])),
        "http://127.0.0.1:18765"
    );
    assert_eq!(
        probe::host_network_url(&doc(&[
            "PSEUDOLIFE_MCP_HOST=100.64.0.2",
            "PSEUDOLIFE_MCP_PORT= 9000 "
        ])),
        "http://100.64.0.2:9000"
    );
    assert_eq!(
        probe::host_network_url(&doc(&["PSEUDOLIFE_MCP_HOST=::"])),
        "http://[::1]:8765"
    );
    // A value it cannot read, or any other network mode: no endpoint.
    for env in [
        &["PSEUDOLIFE_MCP_PORT=8_080"][..],
        &["PSEUDOLIFE_MCP_PORT=0"],
        &["PSEUDOLIFE_MCP_HOST=box.lan"],
    ] {
        assert_eq!(probe::host_network_url(&doc(env)), "", "{env:?}");
    }
    assert_eq!(
        probe::host_network_url(
            &json!({"HostConfig": {"NetworkMode": "bridge"}, "Config": {"Env": []}})
        ),
        ""
    );
    // A dry run from a host-network source reads its health there.
    let mut world = world();
    let mut daemon = daemon_doc(true, "pseudolife-stack", "/x");
    daemon["HostConfig"] = json!({"NetworkMode": "host", "RestartPolicy": {"Name": "no"}});
    daemon["Config"]["Env"]
        .as_array_mut()
        .unwrap()
        .push(json!("PSEUDOLIFE_MCP_PORT=18765"));
    world
        .here
        .docs
        .insert("pseudolife-mcp-daemon".into(), daemon);
    preflight(&mut world, &request()).expect("a plan");
    assert_eq!(world.gets[0], "http://127.0.0.1:18765/health");
    assert_read_only(&world);
}

#[test]
fn remote_commands_are_one_posix_word_after_sh_c() {
    let command = remote::sh(&script(protocol::PROTOCOL));
    assert!(command.starts_with("sh -c '"), "{command}");
    assert!(command.ends_with('\''), "{command}");
    // The script's own single quotes would end the word: it has none.
    assert!(!script(protocol::PROTOCOL).contains('\''));
    assert!(script(protocol::PROTOCOL).contains("exec \"$c\" move-target 2"));
}

#[cfg(unix)]
#[test]
fn an_ssh_timeout_ends_the_run_even_when_a_child_keeps_its_pipes_open() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(format!(
        "move-ssh-timeout-{}",
        uuid::Uuid::new_v4().simple()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    // A ProxyCommand-like child inherits stdout and stderr and outlives ssh.
    let program = dir.join("ssh");
    std::fs::write(&program, "#!/bin/sh\nsleep 30 &\nsleep 60\n").unwrap();
    std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
    let mut ssh = remote::Ssh { program };
    let started = std::time::Instant::now();
    let ran = ssh.run("u@box", "true", b"{}", Duration::from_secs(1));
    let took = started.elapsed();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(ran.lost(), "{ran:?}");
    assert_eq!(
        ran.fault.as_deref(),
        Some("it did not finish within 1 seconds")
    );
    assert!(
        took < Duration::from_secs(10),
        "the run waited {took:?} on the surviving child"
    );
}

#[test]
fn a_daemon_the_gate_cannot_stop_and_an_unauthenticated_target_are_refused() {
    let facts = |dir: &str| Facts {
        daemon: "pseudolife-mcp-daemon".into(),
        data_dir: dir.into(),
        ..Facts::default()
    };
    assert_eq!(super::gate_refusal("target", &facts("/data")), None);
    for dir in ["", "/srv/data", "/data/"] {
        let why = super::gate_refusal("target", &facts(dir)).unwrap();
        assert!(why.contains("not /data") && why.contains("gate"), "{why}");
    }
    // A target daemon that serves without authentication.
    let mut world = world();
    let mut answer = health(TARGET_ID, 60);
    answer["auth"] = json!(false);
    world.wire.far.health = Some(answer);
    let why = preflight(&mut world, &request()).unwrap_err();
    assert!(why.contains("does not require authentication"), "{why}");
    // A source database the fence cannot name.
    let mut source = facts("/data");
    source.running = true;
    source.db = "Pseudolife-Memory".into();
    let why = source_refusal(&source).unwrap();
    assert!(why.contains("cannot name safely"), "{why}");
}
