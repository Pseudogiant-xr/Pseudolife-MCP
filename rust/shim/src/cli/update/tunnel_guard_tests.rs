//! In-process only: a disposable home and layout, and a fake task registry.
//! No test runs systemctl, launchctl or schtasks.
use super::*;
use crate::runtime::{BINARY, UpdateLock};
use std::{cell::RefCell, collections::BTreeMap};

const HEX: &str = "3c3b30db37fcc1368d48917c2405a0b06b94add3faba142553cb8649e81527d7";

struct Fixture {
    path: PathBuf,
    layout: Layout,
}
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("tunnel-guard-test-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&path).unwrap();
        fs::create_dir_all(path.join("home")).unwrap();
        let layout = Layout {
            root: path.join("runtimes"),
            launcher: path.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        };
        Self { path, layout }
    }
    fn home(&self) -> PathBuf {
        self.path.join("home")
    }
    fn launcher(&self) -> String {
        self.layout.launcher.to_string_lossy().into_owned()
    }
    fn store(&self) -> String {
        self.home()
            .join(".pseudolife-mcp")
            .join("tunnel")
            .to_string_lossy()
            .into_owned()
    }
    /// The launcher a Python client leaves: not a native binary.
    fn python_launcher(&self) {
        fs::create_dir_all(self.layout.launcher.parent().unwrap()).unwrap();
        fs::write(
            &self.layout.launcher,
            b"#!/bin/sh\n# python selector\nexec \"$runtime/bin/pseudolife-mcp\" \"$@\"\n",
        )
        .unwrap();
    }
    /// A launcher that already is this layout's native runtime binary.
    fn native_launcher(&self) {
        let binary = self.path.join("fixture-native");
        #[cfg(windows)]
        let header = b"MZ".as_slice();
        #[cfg(target_os = "linux")]
        let header = b"\x7fELF".as_slice();
        #[cfg(target_os = "macos")]
        let header = b"\xcf\xfa\xed\xfe".as_slice();
        let mut bytes = header.to_vec();
        bytes.extend_from_slice(b"1.0.0");
        fs::write(&binary, bytes).unwrap();
        let _lock = UpdateLock::acquire(&self.layout).unwrap();
        runtime::install_binary(
            &self.layout,
            "1.0.0",
            "pseudolife-mcp==1.0.0",
            None,
            &binary,
        )
        .unwrap();
    }
    fn write(&self, relative: &str, text: &str) -> PathBuf {
        let path = relative
            .split('/')
            .fold(self.home(), |path, part| path.join(part));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, text).unwrap();
        path
    }
    /// The unit `render_service` writes for profile `dot` and `program`.
    fn unit(&self, program: &str) -> String {
        unit(&[
            program,
            "tunnel",
            "run",
            "--profile",
            "dot",
            "--profile-dir",
            &self.store(),
        ])
    }
    /// Every file under the fixture, by path, with its bytes.
    fn snapshot(&self) -> BTreeMap<PathBuf, Vec<u8>> {
        fn walk(dir: &Path, into: &mut BTreeMap<PathBuf, Vec<u8>>) {
            for entry in fs::read_dir(dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    walk(&path, into);
                } else {
                    into.insert(path.clone(), fs::read(&path).unwrap());
                }
            }
        }
        let mut files = BTreeMap::new();
        walk(&self.path, &mut files);
        files
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

/// `render_service`'s Linux unit, its `quoted()` included.
fn unit(command: &[&str]) -> String {
    let quoted = |value: &str| {
        format!(
            "\"{}\"",
            value
                .replace('\\', "\\\\")
                .replace('"', "\\\"")
                .replace('%', "%%")
                .replace('$', "$$")
        )
    };
    let launch = command
        .iter()
        .map(|part| quoted(part))
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "# {OWNER}{HEX}\n[Unit]\nDescription=Optional Pseudolife MCP tunnel (dot)\nAfter=network-online.target\n[Service]\nType=simple\nExecStart={launch}\nRestart=on-failure\nRestartSec=5\nUMask=0077\n[Install]\nWantedBy=default.target\n"
    )
}

fn xml(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// What `render_service` writes for macOS (plistlib, XML).
fn plist(program: &str) -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\">\n<dict>\n\t<key>KeepAlive</key>\n\t<dict>\n\t\t<key>SuccessfulExit</key>\n\t\t<false/>\n\t</dict>\n\t<key>Label</key>\n\t<string>pseudolife-tunnel-dot</string>\n\t<key>ProcessType</key>\n\t<string>Background</string>\n\t<key>ProgramArguments</key>\n\t<array>\n\t\t<string>{}</string>\n\t\t<string>tunnel</string>\n\t\t<string>run</string>\n\t\t<string>--profile</string>\n\t\t<string>dot</string>\n\t</array>\n\t<key>PseudolifeTunnelOwner</key>\n\t<string>{OWNER}{HEX}</string>\n\t<key>RunAtLoad</key>\n\t<true/>\n</dict>\n</plist>\n",
        xml(program)
    )
}

/// A task as `schtasks /Query /XML` prints it to a console: UTF-16 with a
/// byte-order mark.
fn task_xml(source: &str, program: &str, arguments: &str) -> Vec<u8> {
    let text = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\r\n<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\r\n  <RegistrationInfo>\r\n    <Source>{source}</Source>\r\n  </RegistrationInfo>\r\n  <Settings>\r\n    <Enabled>false</Enabled>\r\n    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\r\n  </Settings>\r\n  <Actions Context=\"Author\">\r\n    <Exec>\r\n      <Command>{}</Command>\r\n      <Arguments>{}</Arguments>\r\n    </Exec>\r\n  </Actions>\r\n</Task>\r\n",
        xml(program),
        xml(arguments)
    );
    let mut bytes = vec![0xff, 0xfe];
    for unit in text.encode_utf16() {
        bytes.extend_from_slice(&unit.to_le_bytes());
    }
    bytes
}

/// A fake Task Scheduler. It answers only read-only queries and records
/// every call; anything else fails the test.
struct Registry {
    listing: Output,
    xml: Vec<u8>,
    calls: RefCell<Vec<Vec<String>>>,
}
impl Registry {
    fn new(code: i32, listing: &str, xml: Vec<u8>) -> Self {
        Self {
            listing: Output {
                code,
                bytes: listing.as_bytes().to_vec(),
            },
            xml,
            calls: RefCell::new(vec![]),
        }
    }
}
impl Runner for Registry {
    fn run(
        &self,
        _: &Path,
        args: &[&str],
        _: Option<&Path>,
        timeout: Duration,
        _: &[(&str, &str)],
    ) -> Output {
        assert_eq!(
            args.first(),
            Some(&"/Query"),
            "only read-only queries: {args:?}"
        );
        assert!(timeout <= Duration::from_secs(30));
        self.calls
            .borrow_mut()
            .push(args.iter().map(|s| s.to_string()).collect());
        if args.contains(&"/XML") {
            Output {
                code: 0,
                bytes: self.xml.clone(),
            }
        } else {
            Output {
                code: self.listing.code,
                bytes: self.listing.bytes.clone(),
            }
        }
    }
}

/// A runner that must never be reached.
struct Untouched;
impl Runner for Untouched {
    fn run(
        &self,
        _: &Path,
        args: &[&str],
        _: Option<&Path>,
        _: Duration,
        _: &[(&str, &str)],
    ) -> Output {
        panic!("no service manager may be asked here: {args:?}")
    }
}

fn linux() -> Host {
    Host::Linux { system: None }
}
fn windows() -> Host {
    Host::Windows {
        schtasks: PathBuf::from("schtasks.exe"),
    }
}

fn update(f: &Fixture, host: &Host, runner: &dyn Runner) -> Decision {
    decide(&f.layout, host, &f.home(), runner, Mover::Update, || true)
}

fn refused(decision: Decision) -> String {
    match decision {
        Decision::Refuse(line) => line,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

const TASKS: &str = "\"\\Pseudolife Claude Shim\",\"N/A\",\"Ready\"\r\n\"\\pseudolife-tunnel-dot\",\"N/A\",\"Disabled\"\r\n\"\\pseudolife-tunnel-dot\",\"N/A\",\"Disabled\"\r\n\"\\Other\\pseudolife-tunnel-x\",\"N/A\",\"Ready\"\r\n";

#[test]
fn a_registered_user_unit_refuses_the_move_and_changes_nothing() {
    let f = Fixture::new();
    f.python_launcher();
    let unit = f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
    let before = f.snapshot();
    let line = refused(update(&f, &linux(), &Untouched));
    assert_eq!(
        f.snapshot(),
        before,
        "the launcher, the runtimes and the unit are byte-identical"
    );
    assert!(
        line.contains(&format!("systemd user unit {}", unit.display())),
        "{line}"
    );
    assert!(line.contains("Nothing was changed"), "{line}");
    assert!(
        line.contains("the native client cannot run a tunnel yet"),
        "{line}"
    );
    assert!(
        line.contains("rerun the same command without PSEUDOLIFE_SHIM_ENGINE=native (unset PSEUDOLIFE_SHIM_ENGINE"),
        "{line}"
    );
    assert!(
        line.contains(&format!(
            "(pseudolife-mcp tunnel service remove --profile dot --profile-dir {}), then rerun it.",
            quote(&f.store())
        )),
        "{line}"
    );
    assert!(!line.contains('\n'), "one line");
}

#[test]
fn a_disabled_unit_still_refuses_because_it_stays_registered() {
    // `systemctl --user disable` removes only the default.target.wants link;
    // the unit render_service wrote stays, and enabling it again loops.
    let f = Fixture::new();
    f.python_launcher();
    let unit = f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    let wants = f.write(
        ".config/systemd/user/default.target.wants/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    let enabled = refused(update(&f, &linux(), &Untouched));
    fs::remove_file(&wants).unwrap();
    let disabled = refused(update(&f, &linux(), &Untouched));
    assert_eq!(enabled, disabled, "enabled or not, the same unit is named");
    assert!(disabled.contains(&unit.display().to_string()), "{disabled}");
}

#[test]
fn a_root_system_unit_refuses_and_names_the_system_service_removal() {
    let f = Fixture::new();
    f.python_launcher();
    let system = f.path.join("etc-systemd-system");
    fs::create_dir_all(&system).unwrap();
    fs::write(
        system.join("pseudolife-tunnel-dot.service"),
        f.unit(&f.launcher()),
    )
    .unwrap();
    let line = refused(update(
        &f,
        &Host::Linux {
            system: Some(system.clone()),
        },
        &Untouched,
    ));
    assert!(
        line.contains(&format!(
            "systemd system unit {}",
            system.join("pseudolife-tunnel-dot.service").display()
        )),
        "{line}"
    );
    assert!(
        line.contains(&format!(
            "pseudolife-mcp tunnel service remove --profile dot --profile-dir {} --system-service",
            quote(&f.store())
        )),
        "{line}"
    );
    // Not root: the system directory is not read at all.
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
}

#[test]
fn a_saved_profile_without_a_service_moves_as_before() {
    let f = Fixture::new();
    f.python_launcher();
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
    f.write(".pseudolife-mcp/tunnel/dot.key", "fixture");
    f.write(
        ".config/systemd/user/pseudolife-codex-shim.service",
        "[Service]\n",
    );
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
}

#[test]
fn a_service_that_runs_another_program_does_not_move_with_the_launcher() {
    // A pip install with no runtimes layout, or an interpreter command: the
    // move replaces neither, so the service keeps working.
    let f = Fixture::new();
    f.python_launcher();
    let pip = f.path.join("pip-bin").join("pseudolife-mcp");
    fs::create_dir_all(pip.parent().unwrap()).unwrap();
    fs::write(&pip, "#!/usr/bin/python3\n").unwrap();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&pip.to_string_lossy()),
    );
    f.write(
        ".config/systemd/user/pseudolife-tunnel-two.service",
        &unit(&[
            "/usr/bin/python3",
            "-m",
            "pseudolife_memory.cli",
            "tunnel",
            "run",
            "--profile",
            "two",
        ]),
    );
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
    let job = f.write(
        "Library/LaunchAgents/pseudolife-tunnel-dot.plist",
        &plist("/usr/bin/python3"),
    );
    assert_eq!(
        update(&f, &Host::Mac, &Untouched),
        Decision::Proceed(vec![])
    );
    fs::write(&job, plist(&f.launcher())).unwrap();
    refused(update(&f, &Host::Mac, &Untouched));
}

#[test]
fn a_unit_without_the_owner_marker_is_not_this_products() {
    let f = Fixture::new();
    f.python_launcher();
    let marked = f.unit(&f.launcher());
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &marked.replacen(&format!("# {OWNER}{HEX}\n"), "", 1),
    );
    f.write(
        ".config/systemd/user/pseudolife-tunnel-two.service",
        &marked.replace(HEX, &HEX[..63]),
    );
    f.write(
        ".config/systemd/user/pseudolife-tunnel-three.service",
        &format!("[Unit]\n# {OWNER}{HEX}\n"),
    );
    f.write(".config/systemd/user/other-tunnel-dot.service", &marked);
    f.write(".config/systemd/user/pseudolife-tunnel-.service", &marked);
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
}

#[test]
fn an_already_native_machine_moves_on_and_says_how_to_stop_the_failing_service() {
    let f = Fixture::new();
    f.native_launcher();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    let asked = std::cell::Cell::new(false);
    let decision = decide(
        &f.layout,
        &linux(),
        &f.home(),
        &Untouched,
        Mover::Update,
        || {
            asked.set(true);
            true
        },
    );
    assert!(
        !asked.get(),
        "registrations decide only whether a launcher moves"
    );
    let Decision::Proceed(lines) = decision else {
        panic!("an already-native machine is not refused: {decision:?}");
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].contains("cannot start under the native client"),
        "{lines:?}"
    );
    assert!(lines[0].contains("keeps restarting it"), "{lines:?}");
    assert!(
        lines[0].contains("systemctl --user disable --now pseudolife-tunnel-dot.service"),
        "{lines:?}"
    );
    // No service: nothing to say.
    fs::remove_file(
        f.home()
            .join(".config")
            .join("systemd")
            .join("user")
            .join("pseudolife-tunnel-dot.service"),
    )
    .unwrap();
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
}

#[test]
fn an_already_native_machine_names_each_service_managers_stop_command() {
    let f = Fixture::new();
    f.native_launcher();
    f.write(
        "Library/LaunchAgents/pseudolife-tunnel-dot.plist",
        &plist(&f.launcher()),
    );
    let Decision::Proceed(lines) = update(&f, &Host::Mac, &Untouched) else {
        panic!()
    };
    assert!(
        lines[0].contains("launchctl bootout gui/$(id -u)/pseudolife-tunnel-dot and launchctl disable gui/$(id -u)/pseudolife-tunnel-dot"),
        "{lines:?}"
    );
    let registry = Registry::new(
        0,
        TASKS,
        task_xml(&format!("{OWNER}{HEX}"), &f.launcher(), "tunnel run"),
    );
    let Decision::Proceed(lines) = update(&f, &windows(), &registry) else {
        panic!()
    };
    assert!(
        lines[0].contains("schtasks /Change /TN \"\\pseudolife-tunnel-dot\" /Disable"),
        "{lines:?}"
    );
    let system = f.path.join("etc-systemd-system");
    fs::create_dir_all(&system).unwrap();
    fs::write(
        system.join("pseudolife-tunnel-dot.service"),
        f.unit(&f.launcher()),
    )
    .unwrap();
    let Decision::Proceed(lines) = update(
        &f,
        &Host::Linux {
            system: Some(system),
        },
        &Untouched,
    ) else {
        panic!()
    };
    assert!(
        lines[0].contains("systemctl disable --now pseudolife-tunnel-dot.service (as root)"),
        "{lines:?}"
    );
}

#[test]
fn no_launcher_or_no_registration_to_move_asks_no_service_manager() {
    let f = Fixture::new();
    assert_eq!(
        update(&f, &windows(), &Untouched),
        Decision::Proceed(vec![])
    );
    f.python_launcher();
    assert_eq!(
        decide(
            &f.layout,
            &windows(),
            &f.home(),
            &Untouched,
            Mover::Update,
            || false
        ),
        Decision::Proceed(vec![])
    );
}

#[test]
fn the_installer_refusal_names_the_installer_rerun() {
    let f = Fixture::new();
    f.python_launcher();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    let line = refused(decide(
        &f.layout,
        &linux(),
        &f.home(),
        &Untouched,
        Mover::Installer,
        || true,
    ));
    assert!(
        line.contains("rerun the installer without PSEUDOLIFE_SHIM_ENGINE=native"),
        "{line}"
    );
    assert!(
        line.contains("), then rerun the installer. Registered: "),
        "{line}"
    );
}

#[test]
fn an_unreadable_unit_directory_proceeds_and_says_what_was_not_checked() {
    let f = Fixture::new();
    f.python_launcher();
    // A file where the directory should be: read_dir fails without NotFound.
    f.write(".config/systemd/user", "not a directory");
    let Decision::Proceed(notes) = update(&f, &linux(), &Untouched) else {
        panic!("an unreadable directory is not a reason to block the update");
    };
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(notes[0].contains("could not be read"), "{notes:?}");
    assert!(
        notes[0].contains("pseudolife-mcp tunnel service remove"),
        "{notes:?}"
    );
}

#[test]
fn a_registered_launchd_job_refuses() {
    let f = Fixture::new();
    f.python_launcher();
    let job = f.write(
        "Library/LaunchAgents/pseudolife-tunnel-dot.plist",
        &plist(&f.launcher()),
    );
    let line = refused(update(&f, &Host::Mac, &Untouched));
    assert!(
        line.contains(&format!("launchd job {}", job.display())),
        "{line}"
    );
    assert!(
        line.contains("tunnel service remove --profile dot)"),
        "{line}"
    );
    let foreign =
        plist(&f.launcher()).replace("<key>PseudolifeTunnelOwner</key>", "<key>Owner</key>");
    fs::write(&job, foreign).unwrap();
    assert_eq!(
        update(&f, &Host::Mac, &Untouched),
        Decision::Proceed(vec![])
    );
}

#[test]
fn a_registered_scheduled_task_refuses_after_read_only_queries() {
    let f = Fixture::new();
    f.python_launcher();
    let arguments = format!("tunnel run --profile dot --profile-dir \"{}\"", f.store());
    let registry = Registry::new(
        0,
        TASKS,
        task_xml(&format!("{OWNER}{HEX}"), &f.launcher(), &arguments),
    );
    let before = f.snapshot();
    let line = refused(update(&f, &windows(), &registry));
    assert_eq!(f.snapshot(), before);
    assert!(
        line.contains("scheduled task \\pseudolife-tunnel-dot"),
        "{line}"
    );
    assert!(
        line.contains("in PowerShell: Remove-Item Env:PSEUDOLIFE_SHIM_ENGINE"),
        "{line}"
    );
    assert!(
        line.contains(&format!(
            "tunnel service remove --profile dot --profile-dir {})",
            quote(&f.store())
        )),
        "{line}"
    );
    assert_eq!(
        *registry.calls.borrow(),
        vec![
            vec!["/Query", "/FO", "CSV", "/NH"],
            vec!["/Query", "/TN", "\\pseudolife-tunnel-dot", "/XML"],
        ],
        "one listing, then one definition per tunnel task in the root folder"
    );
}

#[test]
fn a_task_definition_printed_without_a_byte_order_mark_is_read() {
    // Captured through a pipe, schtasks /XML prints single-byte text with
    // `\r\r\n` line ends and no byte-order mark (observed on Windows 11).
    let text = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\r\r\n<Task>\r\r\n  <RegistrationInfo>\r\r\n    <Source>{OWNER}{HEX}</Source>\r\r\n  </RegistrationInfo>\r\r\n  <Actions>\r\r\n    <Exec>\r\r\n      <Command>C:\\Users\\&lt;user&gt;\\AppData\\Local\\pseudolife-mcp\\bin\\pseudolife-mcp.exe</Command>\r\r\n    </Exec>\r\r\n  </Actions>\r\r\n</Task>\r\r\n"
    );
    let output = Output {
        code: 0,
        bytes: text.into_bytes(),
    };
    let decoded = decode(&output);
    assert!(task_owned(&decoded));
    assert_eq!(
        task_command(&decoded).unwrap(),
        vec![r"C:\Users\<user>\AppData\Local\pseudolife-mcp\bin\pseudolife-mcp.exe"]
    );
}

#[test]
fn a_task_of_that_name_without_the_marker_moves() {
    let f = Fixture::new();
    f.python_launcher();
    let registry = Registry::new(
        0,
        "\"\\pseudolife-tunnel-dot\",\"N/A\",\"Ready\"\r\n",
        task_xml("someone else", &f.launcher(), "tunnel run"),
    );
    assert_eq!(update(&f, &windows(), &registry), Decision::Proceed(vec![]));
}

#[test]
fn a_task_scheduler_that_does_not_answer_proceeds_with_one_line() {
    let f = Fixture::new();
    f.python_launcher();
    let registry = Registry::new(1, "", vec![]);
    let Decision::Proceed(notes) = update(&f, &windows(), &registry) else {
        panic!("a query that cannot run is not a reason to block the update");
    };
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(
        notes[0].contains("could not be listed (schtasks /Query exited 1)"),
        "{notes:?}"
    );
}

#[test]
fn owner_markers_are_read_exactly() {
    assert!(unit_owned(&format!("# {OWNER}{HEX}\r\n[Unit]\n")));
    assert!(!unit_owned(&format!("# {OWNER}{}\n", HEX.to_uppercase())));
    assert!(!unit_owned(&format!("#{OWNER}{HEX}\n")));
    assert!(!unit_owned(&format!("# {OWNER}{HEX}0\n")));
    assert!(plist_owned(&plist("/x")));
    assert!(!plist_owned(&plist("/x").replace(HEX, &HEX[1..])));
    assert!(task_owned(&format!("<Source>{OWNER}{HEX}</Source>")));
    assert!(!task_owned(&format!("<Source>{OWNER}{HEX} </Source>")));
    assert!(!task_owned(&format!(
        "<Description>{OWNER}{HEX}</Description>"
    )));
    assert!(plain_name("dot-2_a.b"));
    assert!(!plain_name("a b"));
}

#[test]
fn service_commands_are_read_as_render_service_writes_them() {
    let odd = "/home/<user>/a \"b\" 100% $HOME\\x";
    assert_eq!(
        unit_command(&unit(&[odd, "tunnel", "run"])).unwrap(),
        vec![odd, "tunnel", "run"]
    );
    assert_eq!(
        unit_command("ExecStart=/usr/bin/x --flag\n").unwrap(),
        vec!["/usr/bin/x", "--flag"]
    );
    assert_eq!(unit_command("ExecStart=\"/unterminated\n"), None);
    assert_eq!(
        plist_command(&plist("/a & b")).unwrap()[..2],
        ["/a & b", "tunnel"]
    );
    assert_eq!(
        split_windows(
            r#"tunnel run --profile dot --profile-dir "C:\Users\<user>\my store" "quote\"d" trail\\"#
        ),
        vec![
            "tunnel",
            "run",
            "--profile",
            "dot",
            "--profile-dir",
            r"C:\Users\<user>\my store",
            "quote\"d",
            r"trail\\"
        ]
    );
}

#[cfg(unix)]
#[test]
fn a_native_launcher_whose_runtime_is_gone_is_still_native() {
    let f = Fixture::new();
    fs::create_dir_all(f.layout.launcher.parent().unwrap()).unwrap();
    fs::write(&f.layout.launcher, b"ELF fixture").unwrap();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!("an ELF launcher is the native client: nothing to refuse");
    };
    assert!(
        lines[0].contains("cannot start under the native client"),
        "{lines:?}"
    );
}

#[test]
fn each_mover_names_its_own_way_to_stay_and_to_move() {
    let service = Registered {
        kind: Kind::UserUnit,
        name: "pseudolife-tunnel-dot.service".into(),
        place: "systemd user unit /u".into(),
        command: None,
    };
    let checkout = refusal(std::slice::from_ref(&service), false, Mover::Checkout);
    assert!(
        checkout.contains("rerun ops/update.sh or ops/update.ps1 without --native, PSEUDOLIFE_NATIVE_UPDATE and PSEUDOLIFE_NATIVE_CLI"),
        "{checkout}"
    );
    let unattended = refusal(std::slice::from_ref(&service), false, Mover::Unattended);
    assert!(
        unattended.contains("this unattended run leaves it as it is"),
        "{unattended}"
    );
    assert!(
        unattended.contains("then the next run moves it."),
        "{unattended}"
    );
    let update = refusal(std::slice::from_ref(&service), false, Mover::Update);
    assert!(
        update.contains("--daemon-only updates the daemon alone"),
        "{update}"
    );
    for line in [checkout, unattended, update] {
        // What a 0.23.x hand-off parent keeps of a warning.
        let kept: String = line.chars().take(400).collect();
        assert!(kept.contains("Nothing was changed; "), "{kept}");
        assert!(
            !line.contains("keeps starting"),
            "a disabled unit does not start: {line}"
        );
    }
}

mod update {
    use super::super::super::{Options, Update};
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn refuse(_: Mover, _: Option<&Path>) -> Decision {
        Decision::Refuse("fixture refusal; Nothing was changed".into())
    }
    static ASKED: AtomicUsize = AtomicUsize::new(0);
    fn proceed(_: Mover, _: Option<&Path>) -> Decision {
        ASKED.fetch_add(1, Ordering::SeqCst);
        Decision::Proceed(vec!["fixture note".into()])
    }

    fn quiet() -> Update {
        let mut update = Update::new(Options {
            as_json: true,
            ..Options::default()
        });
        update.tunnel_probe = refuse;
        update
    }

    #[test]
    fn the_refusal_is_a_usage_error_that_names_its_fix() {
        let mut update = quiet();
        let error = update.refuse_tunnel_move(None).unwrap_err();
        assert_eq!(error.exit_code, 2);
        assert_eq!(error.message, "fixture refusal; Nothing was changed");
        assert!(!update.tunnel_checked);
    }

    #[test]
    fn the_client_steps_stop_before_the_shim_step_when_refused() {
        let mut update = quiet();
        // `clients` returns at once when this is true (its first statement);
        // the backstop is tested here so a regression cannot reach the real
        // client ladder.
        assert!(update.refused_clients(None));
        assert_eq!(update.report.exit_code, 2);
        assert!(!update.report.ok);
        let clients = update.report.clients.as_ref().unwrap();
        assert_eq!(clients["shim"]["state"], "refused");
        assert_eq!(
            update.report.steps,
            vec!["WARNING: fixture refusal; Nothing was changed".to_string()]
        );
    }

    static MOVER: std::sync::Mutex<Vec<Mover>> = std::sync::Mutex::new(Vec::new());
    fn record(mover: Mover, _: Option<&Path>) -> Decision {
        MOVER.lock().unwrap().push(mover);
        Decision::Refuse("fixture".into())
    }

    #[test]
    fn the_mover_follows_the_run() {
        for (unattended, mode, expected) in [
            (false, "release", Mover::Update),
            (false, "checkout", Mover::Checkout),
            (true, "unattended", Mover::Unattended),
        ] {
            let mut update = quiet();
            update.tunnel_probe = record;
            update.o.unattended = unattended;
            update.report.mode = mode.into();
            update.refuse_tunnel_move(None).unwrap_err();
            assert_eq!(MOVER.lock().unwrap().pop(), Some(expected), "{mode}");
        }
    }

    #[test]
    fn a_passed_probe_is_asked_once_per_run_and_its_lines_are_warnings() {
        let mut update = quiet();
        update.tunnel_probe = proceed;
        let before = ASKED.load(Ordering::SeqCst);
        update.refuse_tunnel_move(None).unwrap();
        update.refuse_tunnel_move(None).unwrap();
        assert_eq!(ASKED.load(Ordering::SeqCst) - before, 1);
        assert_eq!(
            update.report.steps,
            vec!["WARNING: fixture note".to_string()]
        );
    }
}
