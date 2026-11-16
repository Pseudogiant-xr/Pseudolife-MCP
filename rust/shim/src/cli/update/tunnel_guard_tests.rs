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

/// What `render_service` writes for a Linux user unit.
fn unit(profile: &str) -> String {
    format!(
        "# {OWNER}{HEX}\n[Unit]\nDescription=Optional Pseudolife MCP tunnel ({profile})\nAfter=network-online.target\n[Service]\nType=simple\nExecStart=\"/home/<user>/.local/share/pseudolife-mcp/bin/pseudolife-mcp\" \"tunnel\" \"run\" \"--profile\" \"{profile}\" \"--profile-dir\" \"/home/<user>/.pseudolife-mcp/tunnel\"\nRestart=on-failure\nRestartSec=5\nUMask=0077\n[Install]\nWantedBy=default.target\n"
    )
}

/// What `render_service` writes for macOS (plistlib, XML).
fn plist(profile: &str) -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\">\n<dict>\n\t<key>KeepAlive</key>\n\t<dict>\n\t\t<key>SuccessfulExit</key>\n\t\t<false/>\n\t</dict>\n\t<key>Label</key>\n\t<string>pseudolife-tunnel-{profile}</string>\n\t<key>ProcessType</key>\n\t<string>Background</string>\n\t<key>ProgramArguments</key>\n\t<array>\n\t\t<string>/Users/<user>/.local/share/pseudolife-mcp/bin/pseudolife-mcp</string>\n\t</array>\n\t<key>PseudolifeTunnelOwner</key>\n\t<string>{OWNER}{HEX}</string>\n\t<key>RunAtLoad</key>\n\t<true/>\n</dict>\n</plist>\n"
    )
}

/// A task as `schtasks /Query /XML` prints it: UTF-16 with a byte-order mark.
fn task_xml(source: &str) -> Vec<u8> {
    let text = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\r\n<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\r\n  <RegistrationInfo>\r\n    <Source>{source}</Source>\r\n  </RegistrationInfo>\r\n  <Settings>\r\n    <Enabled>false</Enabled>\r\n    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\r\n  </Settings>\r\n</Task>\r\n"
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
    Host::Linux { root: false }
}
fn windows() -> Host {
    Host::Windows {
        schtasks: PathBuf::from("schtasks.exe"),
    }
}

fn refused(decision: Decision) -> String {
    match decision {
        Decision::Refuse(line) => line,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

#[test]
fn a_registered_user_unit_refuses_the_move_and_changes_nothing() {
    let f = Fixture::new();
    f.python_launcher();
    let unit = f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &unit("dot"),
    );
    f.write(".pseudolife-mcp/tunnel/dot.json", "{}");
    let before = f.snapshot();
    let line = refused(decide(&f.layout, &linux(), &f.home(), &Untouched, || true));
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
        line.contains("without PSEUDOLIFE_SHIM_ENGINE=native (unset PSEUDOLIFE_SHIM_ENGINE"),
        "{line}"
    );
    assert!(
        line.contains("pseudolife-mcp tunnel service remove --profile dot,"),
        "{line}"
    );
    assert!(!line.contains('\n'), "one line");
}

#[test]
fn a_disabled_unit_still_refuses_because_it_stays_registered() {
    // `systemctl --user disable` removes only the default.target.wants link;
    // the unit render_service wrote is still there and enabling it loops.
    let f = Fixture::new();
    f.python_launcher();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &unit("dot"),
    );
    assert!(
        !f.home()
            .join(".config")
            .join("systemd")
            .join("user")
            .join("default.target.wants")
            .exists()
    );
    refused(decide(&f.layout, &linux(), &f.home(), &Untouched, || true));
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
    assert_eq!(
        decide(&f.layout, &linux(), &f.home(), &Untouched, || true),
        Decision::Proceed(vec![])
    );
}

#[test]
fn a_unit_without_the_owner_marker_is_not_this_products() {
    let f = Fixture::new();
    f.python_launcher();
    let unmarked = unit("dot").replacen(&format!("# {OWNER}{HEX}\n"), "", 1);
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &unmarked,
    );
    let short = unit("two").replace(HEX, &HEX[..63]);
    f.write(".config/systemd/user/pseudolife-tunnel-two.service", &short);
    let late = format!("[Unit]\n# {OWNER}{HEX}\n");
    f.write(
        ".config/systemd/user/pseudolife-tunnel-three.service",
        &late,
    );
    f.write(
        ".config/systemd/user/other-tunnel-dot.service",
        &unit("dot"),
    );
    assert_eq!(
        decide(&f.layout, &linux(), &f.home(), &Untouched, || true),
        Decision::Proceed(vec![])
    );
}

#[test]
fn an_already_native_launcher_is_not_moved_and_nothing_is_asked() {
    let f = Fixture::new();
    f.native_launcher();
    f.write(
        ".config/systemd/user/pseudolife-tunnel-dot.service",
        &unit("dot"),
    );
    let asked = std::cell::Cell::new(false);
    let decision = decide(&f.layout, &windows(), &f.home(), &Untouched, || {
        asked.set(true);
        true
    });
    assert_eq!(decision, Decision::Proceed(vec![]));
    assert!(
        !asked.get(),
        "registrations are read only when the launcher would move"
    );
}

#[test]
fn no_launcher_or_no_registration_to_move_asks_no_service_manager() {
    let f = Fixture::new();
    assert_eq!(
        decide(&f.layout, &windows(), &f.home(), &Untouched, || true),
        Decision::Proceed(vec![])
    );
    f.python_launcher();
    assert_eq!(
        decide(&f.layout, &windows(), &f.home(), &Untouched, || false),
        Decision::Proceed(vec![])
    );
}

#[test]
fn an_unreadable_unit_directory_proceeds_and_says_what_was_not_checked() {
    let f = Fixture::new();
    f.python_launcher();
    // A file where the directory should be: read_dir fails without NotFound.
    f.write(".config/systemd/user", "not a directory");
    let Decision::Proceed(notes) = decide(&f.layout, &linux(), &f.home(), &Untouched, || true)
    else {
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
        &plist("dot"),
    );
    let line = refused(decide(&f.layout, &Host::Mac, &f.home(), &Untouched, || {
        true
    }));
    assert!(
        line.contains(&format!("launchd job {}", job.display())),
        "{line}"
    );
    let foreign = plist("dot").replace("<key>PseudolifeTunnelOwner</key>", "<key>Owner</key>");
    fs::write(&job, foreign).unwrap();
    assert_eq!(
        decide(&f.layout, &Host::Mac, &f.home(), &Untouched, || true),
        Decision::Proceed(vec![])
    );
}

#[test]
fn a_registered_scheduled_task_refuses_after_read_only_queries() {
    let f = Fixture::new();
    f.python_launcher();
    let listing = "\"\\Pseudolife Claude Shim\",\"N/A\",\"Ready\"\r\n\"\\pseudolife-tunnel-dot\",\"N/A\",\"Disabled\"\r\n\"\\pseudolife-tunnel-dot\",\"N/A\",\"Disabled\"\r\n\"\\Other\\pseudolife-tunnel-x\",\"N/A\",\"Ready\"\r\n";
    let registry = Registry::new(0, listing, task_xml(&format!("{OWNER}{HEX}")));
    let before = f.snapshot();
    let line = refused(decide(&f.layout, &windows(), &f.home(), &registry, || true));
    assert_eq!(f.snapshot(), before);
    assert!(
        line.contains("scheduled task \\pseudolife-tunnel-dot"),
        "{line}"
    );
    assert!(
        line.contains("in PowerShell: Remove-Item Env:PSEUDOLIFE_SHIM_ENGINE"),
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
fn a_task_of_that_name_without_the_marker_moves() {
    let f = Fixture::new();
    f.python_launcher();
    let registry = Registry::new(
        0,
        "\"\\pseudolife-tunnel-dot\",\"N/A\",\"Ready\"\r\n",
        task_xml("someone else"),
    );
    assert_eq!(
        decide(&f.layout, &windows(), &f.home(), &registry, || true),
        Decision::Proceed(vec![])
    );
}

#[test]
fn a_task_scheduler_that_does_not_answer_proceeds_with_one_line() {
    let f = Fixture::new();
    f.python_launcher();
    let registry = Registry::new(1, "", vec![]);
    let Decision::Proceed(notes) = decide(&f.layout, &windows(), &f.home(), &registry, || true)
    else {
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
    assert!(plist_owned(&plist("dot")));
    assert!(!plist_owned(&plist("dot").replace(HEX, &HEX[1..])));
    assert!(task_owned(&format!("<Source>{OWNER}{HEX}</Source>")));
    assert!(!task_owned(&format!("<Source>{OWNER}{HEX} </Source>")));
    assert!(!task_owned(&format!(
        "<Description>{OWNER}{HEX}</Description>"
    )));
    assert_eq!(display_profile("dot-2_a.b"), "dot-2_a.b");
    assert_eq!(display_profile("a b"), "<profile>");
}

#[test]
fn a_root_system_unit_names_the_system_service_switch() {
    let line = refusal(
        &[Registered {
            profile: "dot".into(),
            place: "systemd system unit /etc/systemd/system/pseudolife-tunnel-dot.service".into(),
            system: true,
        }],
        false,
    );
    assert!(
        line.contains("tunnel service remove --profile dot --system-service"),
        "{line}"
    );
}

mod update {
    use super::super::super::{Options, Update};
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn refuse(_: Option<&Path>) -> Decision {
        Decision::Refuse("fixture refusal; Nothing was changed".into())
    }
    static ASKED: AtomicUsize = AtomicUsize::new(0);
    fn proceed(_: Option<&Path>) -> Decision {
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
        update.clients(None, "pseudolife-mcp==0.24.0", None);
        assert_eq!(update.report.exit_code, 2);
        assert!(!update.report.ok);
        let clients = update.report.clients.as_ref().unwrap();
        assert_eq!(clients["shim"]["state"], "refused");
        assert!(
            !update
                .report
                .steps
                .iter()
                .any(|s| s.contains("updating the client side")),
            "{:?}",
            update.report.steps
        );
    }

    #[test]
    fn a_passed_probe_is_asked_once_per_run_and_its_notes_are_warnings() {
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
