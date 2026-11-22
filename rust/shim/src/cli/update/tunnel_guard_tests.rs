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

/// A fake Task Scheduler. It answers only the read-only all-tasks query,
/// `schtasks /Query /XML`, and records every call; anything else fails the
/// test. `names` lists the tasks as the old CSV listing did (first quoted
/// field), each answered with `xml`, as `schtasks` pipes it: each
/// definition after a `<!-- \name -->` comment, in single-byte text.
struct Registry {
    code: i32,
    answer: Vec<u8>,
    calls: RefCell<Vec<Vec<String>>>,
}
impl Registry {
    fn new(code: i32, names: &str, xml: Vec<u8>) -> Self {
        let text = if xml.starts_with(&[0xff, 0xfe]) {
            let units: Vec<u16> = xml[2..]
                .chunks_exact(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect();
            String::from_utf16_lossy(&units)
        } else {
            String::from_utf8_lossy(&xml).into_owned()
        };
        let mut seen: Vec<String> = Vec::new();
        let mut answer = String::from("\r\n<Tasks>\r\n");
        for line in names.lines() {
            let Some(name) = line.strip_prefix('"').and_then(|r| r.split('"').next()) else {
                continue;
            };
            if seen.iter().any(|n| n.eq_ignore_ascii_case(name)) {
                continue;
            }
            seen.push(name.to_owned());
            let body = if product_task(name) {
                text.clone()
            } else {
                "<Task><Actions><Exec><Command>C:\\Windows\\System32\\cmd.exe</Command><Arguments>/c exit 0</Arguments></Exec></Actions></Task>\r\n".to_owned()
            };
            answer.push_str(&format!("\r\n\r\n<!-- {name} -->\r\n{body}"));
        }
        answer.push_str("</Tasks>\r\n");
        Self {
            code,
            answer: if code == 0 {
                answer.into_bytes()
            } else {
                vec![]
            },
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
            args,
            ["/Query", "/XML"],
            "only the read-only all-tasks query"
        );
        assert!(timeout <= Duration::from_secs(30));
        self.calls
            .borrow_mut()
            .push(args.iter().map(|s| s.to_string()).collect());
        Output {
            code: self.code,
            bytes: self.answer.clone(),
        }
    }
}

/// `unit_command` for a system unit (no home for `%h`).
fn unit_cmd(text: &str) -> Option<Vec<String>> {
    unit_command(text, None)
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
    Host::Linux {
        system: None,
        root: false,
    }
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
        line.starts_with(
            "this machine stays on the Python client: a tunnel is registered to start by itself. Nothing was changed; to stay on it, rerun the same command"
        ),
        "{line}"
    );
    assert!(
        line.contains(
            "The native client has no tunnel service command to manage that registration after the move."
        ) && line.contains("Run it in the foreground with pseudolife-mcp tunnel run.")
            && line.matches("foreground").count() == 1
            && line.contains("has no automatic or background start")
            && line.contains("Removing autostart means the tunnel will no longer start on its own")
            && !line.contains(concat!("cannot run ", "a tunnel yet")),
        "{line}"
    );
    assert!(
        line.contains("rerun the same command without PSEUDOLIFE_SHIM_ENGINE=native (unset PSEUDOLIFE_SHIM_ENGINE"),
        "{line}"
    );
    assert!(
        line.contains(&format!(
            "(pseudolife-mcp tunnel service remove --profile dot --profile-dir {}), then rerun it.",
            quote(&f.store(), false)
        )),
        "{line}"
    );
    assert!(!line.contains('\n'), "one line");
}

#[test]
fn a_disabled_unit_still_refuses_because_it_stays_registered() {
    // `systemctl --user disable` removes only the default.target.wants link;
    // the unit render_service wrote stays and can still be enabled.
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
            root: true,
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
            quote(&f.store(), false)
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
fn a_unit_without_the_owner_marker_is_only_warned_about() {
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
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!("not this product's marker: never a refusal");
    };
    assert_eq!(lines.len(), 4, "{lines:?}");
    assert!(
        lines
            .iter()
            .all(|l| l.contains("looks like a tunnel service made by hand")),
        "{lines:?}"
    );
}

#[test]
fn an_already_native_machine_moves_on_and_names_the_service_removal_command() {
    let f = Fixture::new();
    f.native_launcher();
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
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
    let unit = f
        .home()
        .join(".config")
        .join("systemd")
        .join("user")
        .join("pseudolife-tunnel-dot.service");
    assert!(
        lines[0].contains("can run a tunnel in the foreground and has no tunnel service command")
            && !lines[0].contains(concat!("cannot run ", "a tunnel yet")),
        "{lines:?}"
    );
    assert!(
        lines[0].contains(&format!(
            "Remove it with: systemctl --user disable --now pseudolife-tunnel-dot.service && rm {} && systemctl --user daemon-reload ; the Python client's record of it, {}, can be deleted too",
            quote(&unit.to_string_lossy(), false),
            quote(
                &PathBuf::from(f.store())
                    .join("pseudolife-tunnel-dot.service-owner")
                    .to_string_lossy(),
                false
            )
        )),
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
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
    f.write(
        "Library/LaunchAgents/pseudolife-tunnel-dot.plist",
        &plist(&f.launcher()),
    );
    let Decision::Proceed(lines) = update(&f, &Host::Mac, &Untouched) else {
        panic!()
    };
    assert!(
        lines[0].contains(
            // An already-unloaded job still gets its plist removed.
            "Remove it with: launchctl bootout gui/$(id -u)/pseudolife-tunnel-dot ; rm "
        ),
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
        lines[0].contains("can run a tunnel in the foreground and has no tunnel service command")
            && !lines[0].contains("fails at each logon")
            && lines[0]
                .contains("Remove it with: schtasks /Delete /TN \\pseudolife-tunnel-dot /F ;"),
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
            root: true,
        },
        &Untouched,
    ) else {
        panic!()
    };
    assert!(
        lines[0].contains(
            "It is a system unit, so only root can change it. Remove it with: systemctl disable --now pseudolife-tunnel-dot.service && rm "
        ),
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
    let Decision::Proceed(lines) = update(&f, &Host::Mac, &Untouched) else {
        panic!("not this product's marker: never a refusal");
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines
            .iter()
            .all(|l| l.contains("looks like a tunnel service made by hand")),
        "{lines:?}"
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
            quote(&f.store(), true)
        )),
        "{line}"
    );
    assert_eq!(
        *registry.calls.borrow(),
        vec![vec!["/Query", "/XML"]],
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
        task_command(&decoded, |_| None).unwrap(),
        vec![r"C:\Users\<user>\AppData\Local\pseudolife-mcp\bin\pseudolife-mcp.exe"]
    );
}

#[test]
fn a_task_of_that_name_without_the_marker_is_only_warned_about() {
    let f = Fixture::new();
    f.python_launcher();
    let registry = Registry::new(
        0,
        "\"\\pseudolife-tunnel-dot\",\"N/A\",\"Ready\"\r\n",
        task_xml("someone else", &f.launcher(), "tunnel run"),
    );
    let Decision::Proceed(lines) = update(&f, &windows(), &registry) else {
        panic!("not this product's marker: never a refusal");
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines
            .iter()
            .all(|l| l.contains("looks like a tunnel service made by hand")),
        "{lines:?}"
    );
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
        notes[0].contains("could not be read (schtasks could not be started)"),
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
        unit_cmd(&unit(&[odd, "tunnel", "run"])).unwrap(),
        vec![odd, "tunnel", "run"]
    );
    assert_eq!(
        unit_cmd("ExecStart=/usr/bin/x --flag\n").unwrap(),
        vec!["/usr/bin/x", "--flag"]
    );
    assert_eq!(unit_cmd("ExecStart=\"/unterminated\n"), None);
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
    fs::write(&f.layout.launcher, b"\x7fELF fixture").unwrap();
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &f.unit(&f.launcher()),
    );
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!("an ELF launcher is the native client: nothing to refuse");
    };
    assert!(
        lines[0].contains("uses this machine's native launcher"),
        "{lines:?}"
    );
}

#[test]
fn each_mover_names_its_own_way_to_stay_and_to_move() {
    let service = Registered {
        kind: Kind::UserUnit,
        name: "pseudolife-tunnel-dot.service".into(),
        place: "systemd user unit /home/alexandra/.config/systemd/user/pseudolife-tunnel-dot.service"
            .into(),
        file: None,
        command: Some(vec![
            "/home/alexandra/.local/bin/pseudolife-mcp".into(),
            "tunnel".into(),
            "run".into(),
            "--profile".into(),
            "dot".into(),
            "--profile-dir".into(),
            "/home/alexandra/.local/share/pseudolife/tunnel profiles".into(),
        ]),
    };
    let checkout = refusal(std::slice::from_ref(&service), false, Mover::Checkout);
    assert!(
        checkout.contains("rerun ops/update.sh or ops/update.ps1 without --native, PSEUDOLIFE_NATIVE_UPDATE and PSEUDOLIFE_NATIVE_CLI"),
        "{checkout}"
    );
    let unattended = refusal(std::slice::from_ref(&service), false, Mover::Unattended);
    assert!(
        unattended.contains("Nothing was changed; this unattended run leaves the client as it is"),
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
            line.contains("--profile-dir '/home/alexandra/.local/share/pseudolife/tunnel profiles'"),
            "{line}"
        );
        assert!(
            !line.contains("keeps starting"),
            "a disabled unit does not start: {line}"
        );
    }
}

#[test]
fn a_native_machine_without_a_tunnel_store_asks_no_service_manager() {
    let f = Fixture::new();
    f.native_launcher();
    // Untouched panics if schtasks were asked.
    assert_eq!(
        update(&f, &windows(), &Untouched),
        Decision::Proceed(vec![])
    );
}

#[test]
fn a_task_command_that_did_not_decode_still_counts_as_the_launcher() {
    // Piped schtasks output is single-byte in the console code page: a home
    // folder with an accented letter is not valid UTF-8 and decodes lossily.
    let f = Fixture::new();
    f.python_launcher();
    let mut xml = format!(
        "<Task><RegistrationInfo><Source>{OWNER}{HEX}</Source></RegistrationInfo><Actions><Exec><Command>C:\\Users\\&lt;user&gt;\\Zo"
    )
    .into_bytes();
    xml.push(0xeb);
    xml.extend_from_slice(b"\\AppData\\Local\\pseudolife-mcp\\bin\\pseudolife-mcp.exe</Command></Exec></Actions></Task>");
    let registry = Registry::new(0, TASKS, xml);
    refused(update(&f, &windows(), &registry));
    // A numeric character reference is not the path either.
    let registry = Registry::new(
        0,
        TASKS,
        format!("<Task><RegistrationInfo><Source>{OWNER}{HEX}</Source></RegistrationInfo><Actions><Exec><Command>C:\\Users\\&lt;user&gt;\\Zo&#235;\\x.exe</Command></Exec></Actions></Task>").into_bytes(),
    );
    refused(update(&f, &windows(), &registry));
}

#[cfg(windows)]
#[test]
fn a_windows_launcher_is_python_only_with_a_distlib_selector() {
    use std::io::Write;
    let f = Fixture::new();
    fs::create_dir_all(f.layout.launcher.parent().unwrap()).unwrap();
    let mut distlib = b"MZ stub".to_vec();
    let mut zipped = std::io::Cursor::new(Vec::new());
    {
        let mut writer = zip::ZipWriter::new(&mut zipped);
        writer
            .start_file("__main__.py", zip::write::SimpleFileOptions::default())
            .unwrap();
        writer.write_all(b"import selector\n").unwrap();
        writer.finish().unwrap();
    }
    distlib.extend_from_slice(zipped.get_ref());
    fs::write(&f.layout.launcher, &distlib).unwrap();
    assert!(
        !native_magic(&f.layout.launcher),
        "a distlib launcher is the Python client"
    );
    fs::write(&f.layout.launcher, b"MZ native fixture").unwrap();
    assert!(
        native_magic(&f.layout.launcher),
        "a PE file without a selector is native"
    );
}

#[test]
fn the_root_unit_removal_pastes_and_says_why_root() {
    let f = Fixture::new();
    f.native_launcher();
    let system = f.path.join("etc-systemd-system");
    fs::create_dir_all(&system).unwrap();
    let unit = system.join("pseudolife-tunnel-dot.service");
    fs::write(&unit, f.unit(&f.launcher())).unwrap();
    let Decision::Proceed(lines) = update(
        &f,
        &Host::Linux {
            system: Some(system),
            root: true,
        },
        &Untouched,
    ) else {
        panic!()
    };
    assert!(
        lines[0].contains(&format!(
            "It is a system unit, so only root can change it. Remove it with: systemctl disable --now pseudolife-tunnel-dot.service && rm {} && systemctl daemon-reload ;",
            quote(&unit.to_string_lossy(), false)
        )),
        "{lines:?}"
    );
    assert!(!lines[0].contains("as root,"), "{lines:?}");
}

#[test]
fn schtasks_failures_are_said_as_what_happened() {
    let started = |code: i32, bytes: &[u8]| {
        failed(&Output {
            code,
            bytes: bytes.to_vec(),
        })
    };
    assert_eq!(started(124, b""), "schtasks did not answer within 30 s");
    assert_eq!(started(1, b""), "schtasks could not be started");
    assert_eq!(
        started(1, b"ERROR: Access is denied.\r\n"),
        "schtasks /Query /XML exited 1"
    );
    assert_eq!(started(2, b""), "schtasks /Query /XML exited 2");
}

#[test]
fn the_could_not_check_note_names_a_stop_command_and_a_public_page() {
    let note = unchecked("x could not be read", Kind::UserUnit);
    assert!(
        note.contains("disable it with systemctl --user disable --now <unit>"),
        "{note}"
    );
    assert!(note.contains("To remove the registration"), "{note}");
    assert!(note.contains(NATIVE_CLIENT_DOCS), "{note}");
    assert!(
        !note.contains("docs/guide/configuration.md describes"),
        "{note}"
    );
    assert!(unchecked("x", Kind::Task).contains("schtasks /Change /TN <task> /Disable"));
    assert!(unchecked("x", Kind::LaunchAgent).contains("launchctl bootout gui/$(id -u)/<label>"));
}

#[test]
fn unit_files_are_read_from_home_even_when_userprofile_is_set() {
    // The Python client wrote them under Path.home(), which is HOME; a WSL
    // session may export a Windows USERPROFILE as well (issue #1632 review).
    let env = |key: &str| match key {
        "HOME" => Some(std::ffi::OsString::from("/home/u")),
        "USERPROFILE" => Some(std::ffi::OsString::from("/mnt/c/Users/u")),
        _ => None,
    };
    assert_eq!(definition_home(false, env), PathBuf::from("/home/u"));
    let empty = |key: &str| (key == "HOME").then(std::ffi::OsString::new);
    assert_eq!(
        definition_home(false, empty),
        dirs::home_dir().unwrap_or_default()
    );
}

/// A bare ExecStart word: systemd decodes backslashes, so a Windows test
/// path (the fixture launcher there) is written as systemd would read it.
fn escaped(path: &str) -> String {
    if path.contains('\\') {
        format!("\"{}\"", path.replace('\\', "\\\\"))
    } else {
        path.to_owned()
    }
}

#[test]
fn a_service_made_by_hand_is_warned_about_never_refused() {
    let f = Fixture::new();
    f.python_launcher();
    let launcher = f.launcher();
    // No owner marker, any name: the command is this launcher, then tunnel run.
    let hand = f.write(
        ".config/systemd/user/my-chatgpt-tunnel.service",
        &format!(
            "[Service]\nExecStart={} tunnel run --profile dot\nRestart=always\n",
            escaped(&launcher)
        ),
    );
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!("a hand-made service is not this product's signal: no refusal");
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].starts_with(&format!(
            "systemd user unit {} looks like a tunnel service made by hand",
            hand.display()
        )),
        "{lines:?}"
    );
    assert!(
        lines[0].contains("can run a tunnel in the foreground and has no tunnel service command")
            && !lines[0].contains("fails at each start"),
        "{lines:?}"
    );
    assert!(
        lines[0].contains(
            "Disable it with: systemctl --user disable --now my-chatgpt-tunnel.service ; "
        ),
        "{lines:?}"
    );
    // This run moves the client: no rerun hint, which would be false here.
    assert!(
        lines[0].ends_with(&format!(
            "can then be deleted. This run moves the client now; to go back to the Python client see {NATIVE_CLIENT_DOCS}"
        )),
        "{lines:?}"
    );
    assert!(!lines[0].contains("rerun"), "{lines:?}");
    assert!(
        !lines[0].contains("&& rm"),
        "what the user wrote is disabled, not deleted: {lines:?}"
    );
    // The installer's preflight may still say how to keep the Python client.
    let Decision::Proceed(installer) = decide(
        &f.layout,
        &linux(),
        &f.home(),
        &Untouched,
        Mover::Installer,
        || true,
    ) else {
        panic!()
    };
    assert!(
        installer[0].ends_with(
            "can then be deleted. (or keep the Python client: rerun the installer without PSEUDOLIFE_SHIM_ENGINE=native)"
        ),
        "{installer:?}"
    );
    // On a native machine the same unit is still named, without the stay hint.
    let g = Fixture::new();
    g.native_launcher();
    g.write(
        ".config/systemd/user/my-chatgpt-tunnel.service",
        &format!(
            "[Service]\nExecStart={} tunnel run\n",
            escaped(&g.launcher())
        ),
    );
    let Decision::Proceed(lines) = update(&g, &linux(), &Untouched) else {
        panic!()
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].contains("can run a tunnel in the foreground and has no tunnel service command")
            && !lines[0].contains("fails at each start"),
        "{lines:?}"
    );
}

#[test]
fn a_unit_that_only_mentions_the_launcher_is_not_a_tunnel_service() {
    let f = Fixture::new();
    f.python_launcher();
    let launcher = f.launcher();
    f.write(
        ".config/systemd/user/comment.service",
        &format!("# runs {launcher} tunnel run\n[Service]\nExecStart=/usr/bin/true\n"),
    );
    f.write(
        ".config/systemd/user/other-command.service",
        &format!("[Service]\nExecStartPre={launcher} tunnel run\nExecStart={launcher} serve\n"),
    );
    f.write(
        ".config/systemd/user/another-program.service",
        "[Service]\nExecStart=/usr/local/bin/pseudolife-mcp tunnel run\n",
    );
    f.write(
        "Library/LaunchAgents/other.plist",
        &plist("/usr/bin/true").replace("<key>PseudolifeTunnelOwner</key>", "<key>Owner</key>"),
    );
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
    assert_eq!(
        update(&f, &Host::Mac, &Untouched),
        Decision::Proceed(vec![])
    );
}

#[test]
fn a_hand_made_task_of_that_name_is_warned_about() {
    let f = Fixture::new();
    f.python_launcher();
    let registry = Registry::new(
        0,
        TASKS,
        task_xml("made by hand", &f.launcher(), "tunnel run --profile dot"),
    );
    let Decision::Proceed(lines) = update(&f, &windows(), &registry) else {
        panic!()
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].starts_with(
            "scheduled task \\pseudolife-tunnel-dot looks like a tunnel service made by hand"
        ),
        "{lines:?}"
    );
    assert!(
        lines[0].contains(
            "Disable it with: schtasks /Change /TN \\pseudolife-tunnel-dot /Disable ; the task can then be deleted too."
        ),
        "{lines:?}"
    );
}

#[test]
fn exec_start_is_read_with_the_spacing_and_prefixes_systemd_accepts() {
    // A marked unit edited to run another program, in valid systemd syntax,
    // is a near miss: it must not count as this launcher.
    for line in [
        " ExecStart=/usr/bin/true",
        "ExecStart = /usr/bin/true",
        "ExecStart=-/usr/bin/true",
        "\tExecStart=  !/usr/bin/true",
    ] {
        assert_eq!(
            unit_cmd(&format!("[Service]\n{line}\n")).unwrap(),
            vec!["/usr/bin/true"],
            "{line}"
        );
    }
    // systemd strips single quotes too; a bare word it would decode further
    // is unreadable, which counts as the launcher.
    assert_eq!(
        unit_cmd("ExecStart = '/home/u/x y/pseudolife-mcp' tunnel run\n").unwrap(),
        vec!["/home/u/x y/pseudolife-mcp", "tunnel", "run"]
    );
    assert_eq!(
        unit_cmd("ExecStart=/home/u/x\\ y/pseudolife-mcp tunnel run\n"),
        None
    );
    assert_eq!(unit_cmd("ExecStart=/home/u/it's tunnel run\n"), None);
    // A later word that does not decode is never printed as a value: the
    // profile comes from the unit's name and no --profile-dir is guessed.
    let f = Fixture::new();
    f.python_launcher();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &format!(
            "# {OWNER}{HEX}\n[Service]\nExecStart={} tunnel run --profile \\x64ot --profile-dir /x\\ y\n",
            escaped(&f.launcher())
        ),
    );
    let line = refused(update(&f, &linux(), &Untouched));
    assert!(
        line.contains("(pseudolife-mcp tunnel service remove --profile dot)"),
        "{line}"
    );
    // A later word that does not decode leaves a known program known.
    assert_eq!(
        unit_cmd("ExecStart=/usr/bin/python3 /opt/tunnel\\x20runner/launch.py\n").unwrap()[0],
        "/usr/bin/python3"
    );
    assert_eq!(
        unit_cmd("ExecStart=/usr/bin/true \"unterminated\n").unwrap()[0],
        "/usr/bin/true"
    );
    let f = Fixture::new();
    f.python_launcher();
    let quoted = format!(
        "# {OWNER}{HEX}\n[Service]\nExecStart = '{}' tunnel run\n",
        f.launcher().replace('\\', "\\\\")
    );
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &quoted,
    );
    refused(update(&f, &linux(), &Untouched));
    let marked = format!("# {OWNER}{HEX}\n[Service]\nExecStart = /usr/bin/true\n");
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &marked,
    );
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
}

#[test]
fn an_already_native_machine_says_what_it_could_not_check() {
    let f = Fixture::new();
    f.native_launcher();
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
    let registry = Registry::new(1, "", vec![]);
    let Decision::Proceed(notes) = update(&f, &windows(), &registry) else {
        panic!()
    };
    assert_eq!(notes.len(), 1, "{notes:?}");
    assert!(
        notes[0].contains("could not be read (schtasks could not be started)"),
        "{notes:?}"
    );
    assert!(
        notes[0].contains("can run a tunnel in the foreground and has no tunnel service command")
            && !notes[0].contains("fails at each start"),
        "{notes:?}"
    );
}

#[test]
fn a_user_unit_naming_the_launcher_through_percent_h_is_warned_about() {
    // %h is the form systemd's own documentation steers user units to.
    let f = Fixture::new();
    f.python_launcher();
    f.write(
        ".config/systemd/user/chatgpt.service",
        &format!("[Service]\nExecStart=%h/../bin/{BINARY} tunnel run\n"),
    );
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!()
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].contains("looks like a tunnel service made by hand"),
        "{lines:?}"
    );
    // %% is a literal percent sign, and other specifiers are left as they
    // read, so they never match by accident.
    let home = Path::new("/home/u");
    assert_eq!(
        unit_command("ExecStart=%h/bin/x 100%% %u\n", Some(home)).unwrap(),
        vec!["/home/u/bin/x", "100%", "%u"]
    );
    assert_eq!(unit_cmd("ExecStart=%h/bin/x\n").unwrap(), vec!["%h/bin/x"]);
}

#[test]
fn a_system_unit_made_by_hand_is_warned_about_without_root() {
    // A unit with User= may run this user's launcher; only root installs
    // the product's own system units, so off root only hand-made ones count.
    let f = Fixture::new();
    f.python_launcher();
    let system = f.path.join("etc-systemd-system");
    fs::create_dir_all(&system).unwrap();
    fs::write(
        system.join("dot-tunnel.service"),
        format!(
            "[Service]\nUser=u\nExecStart={} tunnel run\n",
            escaped(&f.launcher())
        ),
    )
    .unwrap();
    fs::write(
        system.join("pseudolife-tunnel-root.service"),
        f.unit(&f.launcher()),
    )
    .unwrap();
    let host = Host::Linux {
        system: Some(system),
        root: false,
    };
    let Decision::Proceed(lines) = update(&f, &host, &Untouched) else {
        panic!("off root a system unit never refuses this user's move");
    };
    assert_eq!(lines.len(), 2, "{lines:?}");
    assert!(
        lines.iter().all(|l| l.contains("made by hand")),
        "{lines:?}"
    );
    assert!(
        lines.iter().any(|l| l.contains(
            "It is a system unit, so only root can change it. Disable it with: systemctl disable --now dot-tunnel.service ;"
        )),
        "{lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn a_linked_unit_is_read_for_hand_made_services_never_as_the_products() {
    let f = Fixture::new();
    f.python_launcher();
    let dotfiles = f.path.join("dotfiles");
    fs::create_dir_all(&dotfiles).unwrap();
    fs::write(
        dotfiles.join("tunnel.service"),
        format!("[Service]\nExecStart={} tunnel run\n", f.launcher()),
    )
    .unwrap();
    fs::write(dotfiles.join("marked.service"), f.unit(&f.launcher())).unwrap();
    let units = f.home().join(".config").join("systemd").join("user");
    fs::create_dir_all(&units).unwrap();
    std::os::unix::fs::symlink(
        dotfiles.join("tunnel.service"),
        units.join("tunnel.service"),
    )
    .unwrap();
    std::os::unix::fs::symlink(
        dotfiles.join("marked.service"),
        units.join("pseudolife-tunnel-dot.service"),
    )
    .unwrap();
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!("a link is never the product's own unit, so it never refuses");
    };
    assert_eq!(lines.len(), 2, "{lines:?}");
    assert!(
        lines.iter().all(|l| l.contains("made by hand")),
        "{lines:?}"
    );
}

#[test]
fn an_at_prefixed_exec_start_drops_its_argv0() {
    assert_eq!(
        unit_cmd("ExecStart=@/usr/bin/x x-name tunnel run\n").unwrap(),
        vec!["/usr/bin/x", "tunnel", "run"]
    );
    assert_eq!(
        unit_cmd("ExecStart=-@/usr/bin/x x-name\n").unwrap(),
        vec!["/usr/bin/x"]
    );
}

#[test]
fn a_task_command_with_environment_variables_is_expanded() {
    let env =
        |key: &str| (key == "LOCALAPPDATA").then(|| r"C:\Users\<user>\AppData\Local".to_owned());
    assert_eq!(
        expand_percent(r"%LOCALAPPDATA%\pseudolife-mcp\bin\pseudolife-mcp.exe", env),
        r"C:\Users\<user>\AppData\Local\pseudolife-mcp\bin\pseudolife-mcp.exe"
    );
    assert_eq!(
        expand_percent("%UNSET%\\x.exe 100%", env),
        "%UNSET%\\x.exe 100%"
    );
    let xml = "<Task><Actions><Exec><Command>%LOCALAPPDATA%\\pseudolife-mcp\\bin\\pseudolife-mcp.exe</Command><Arguments>tunnel run</Arguments></Exec></Actions></Task>";
    assert_eq!(
        task_command(xml, env).unwrap(),
        vec![
            r"C:\Users\<user>\AppData\Local\pseudolife-mcp\bin\pseudolife-mcp.exe",
            "tunnel",
            "run"
        ]
    );
}

#[test]
fn a_tunnel_run_under_another_program_is_not_a_hand_made_service() {
    // The launcher as an argument, or tunnel run given to another program.
    let f = Fixture::new();
    f.python_launcher();
    f.write(
        ".config/systemd/user/wrapped.service",
        &format!(
            "[Service]\nExecStart=/usr/bin/python3 {} tunnel run\n",
            escaped(&f.launcher())
        ),
    );
    assert_eq!(update(&f, &linux(), &Untouched), Decision::Proceed(vec![]));
    let other = "<Task><Actions><Exec><Command>C:\\Windows\\System32\\cmd.exe</Command><Arguments>tunnel run --profile dot</Arguments></Exec></Actions></Task>";
    let registry = Registry::new(
        0,
        "\"\\Some other task\",\"N/A\",\"Ready\"\r\n",
        other.as_bytes().to_vec(),
    );
    assert_eq!(update(&f, &windows(), &registry), Decision::Proceed(vec![]));
}

#[test]
fn every_task_is_read_for_hand_made_services_in_one_query() {
    // A hand-made task almost never uses the product's name.
    let f = Fixture::new();
    f.python_launcher();
    let mine = format!(
        "<Task><Actions><Exec><Command>{}</Command><Arguments>tunnel run --profile dot</Arguments></Exec></Actions></Task>",
        xml(&f.launcher())
    );
    let unrelated = "<Task><Actions><Exec><Command>C:\\Windows\\System32\\cmd.exe</Command></Exec></Actions></Task>";
    let answer = format!(
        "\r\n<Tasks>\r\n<!-- \\Microsoft\\Windows\\Defrag\\ScheduledDefrag -->\r\n{unrelated}\r\n<!-- \\My Tasks\\ChatGPT tunnel -->\r\n{mine}\r\n</Tasks>\r\n"
    );
    let defs = task_definitions(&answer);
    assert_eq!(defs.len(), 2, "{defs:?}");
    assert_eq!(defs[1].0, "\\My Tasks\\ChatGPT tunnel");
    struct One(Vec<u8>);
    impl Runner for One {
        fn run(
            &self,
            _: &Path,
            args: &[&str],
            _: Option<&Path>,
            _: Duration,
            _: &[(&str, &str)],
        ) -> Output {
            assert_eq!(args, ["/Query", "/XML"]);
            Output {
                code: 0,
                bytes: self.0.clone(),
            }
        }
    }
    let Decision::Proceed(lines) = update(&f, &windows(), &One(answer.into_bytes())) else {
        panic!()
    };
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].contains(
            "Disable it with: (PowerShell) schtasks /Change /TN '\\My Tasks\\ChatGPT tunnel' /Disable ;"
        ),
        "{lines:?}"
    );
}

#[test]
fn names_in_printed_commands_are_quoted() {
    let f = Fixture::new();
    f.python_launcher();
    f.write(
        ".config/systemd/user/a;b.service",
        &format!(
            "[Service]\nExecStart={} tunnel run\n",
            escaped(&f.launcher())
        ),
    );
    let Decision::Proceed(lines) = update(&f, &linux(), &Untouched) else {
        panic!()
    };
    assert!(
        lines[0].contains("Disable it with: systemctl --user disable --now 'a;b.service' ;"),
        "{lines:?}"
    );
}

#[test]
fn printed_paths_survive_a_paste() {
    assert_eq!(
        quote("/a\\b", false),
        "'/a\\b'",
        "a backslash is plain only to PowerShell"
    );
    assert_eq!(quote(r"C:\x\y", true), r"C:\x\y");
    assert_eq!(
        quote("/home/u/.pseudolife-mcp/tunnel", false),
        "/home/u/.pseudolife-mcp/tunnel"
    );
    assert_eq!(quote("/home/u/my store", false), "'/home/u/my store'");
    assert_eq!(quote("/a/it's;$x", false), "'/a/it'\\''s;$x'");
    assert_eq!(
        quote(r"C:\Users\<user>\it's (x)", true),
        r"'C:\Users\<user>\it''s (x)'"
    );
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

    static DAEMON: std::sync::Mutex<Vec<Mover>> = std::sync::Mutex::new(Vec::new());
    fn record_daemon(mover: Mover, _: Option<&Path>) -> Decision {
        DAEMON.lock().unwrap().push(mover);
        Decision::Refuse("fixture refusal".into())
    }

    #[test]
    fn an_unattended_daemon_update_goes_on_alone_and_warns() {
        // The daemon is not left behind every night for a client-side
        // service: the run becomes --daemon-only and says why.
        let mut update = quiet();
        update.tunnel_probe = record_daemon;
        update.o.unattended = true;
        update.refuse_tunnel_move_before_daemon().unwrap();
        assert_eq!(DAEMON.lock().unwrap().pop(), Some(Mover::UnattendedDaemon));
        assert!(update.o.daemon_only, "the client steps are skipped");
        assert!(update.tunnel_checked);
        assert_eq!(update.report.exit_code, 0);
        assert_eq!(
            update.report.steps,
            vec!["WARNING: fixture refusal".to_string()]
        );
        // An attended run at the same point still refuses.
        let mut attended = quiet();
        attended.tunnel_probe = record_daemon;
        assert_eq!(
            attended
                .refuse_tunnel_move_before_daemon()
                .unwrap_err()
                .exit_code,
            2
        );
        assert_eq!(DAEMON.lock().unwrap().pop(), Some(Mover::Update));
        assert!(!attended.o.daemon_only);
    }

    #[test]
    fn the_backstop_keeps_the_client_steps_out_after_a_daemon_only_turn() {
        let mut update = quiet();
        update.tunnel_probe = record_daemon;
        update.o.unattended = true;
        update.refuse_tunnel_move_before_daemon().unwrap();
        assert_eq!(update.tunnel_refused.as_deref(), Some("fixture refusal"));
        // Even if a later step reached the client steps, they stay out.
        assert!(update.refused_clients(None));
        assert_eq!(
            update.report.clients.as_ref().unwrap()["shim"]["state"],
            "refused"
        );
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
