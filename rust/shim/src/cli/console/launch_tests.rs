//! The launch against stubs: a test build starts nothing but a program in
//! the stub directory its own test registered (#1509).
use super::*;
use std::panic::{AssertUnwindSafe, catch_unwind};

#[test]
fn windows_launch_failures_keep_the_cause_and_recovery_without_spawning() {
    let result = spawn_result("C:/stub/browser.exe", || {
        Err("could not start C:/stub/browser.exe (permission denied)".to_owned())
    });
    assert!(
        matches!(result, Err(Some(ref why)) if why.contains("permission denied") && why.contains("printed link")),
        "{result:?}"
    );
    for code in [0, 2, 3, 5, 8, 26, 27, 28, 29, 30, 31, 32] {
        let result = shell_result(code);
        assert!(
            matches!(result, Err(Some(ref why)) if why.contains("default browser") && why.contains("printed link")),
            "{code}: {result:?}"
        );
    }
    assert_eq!(shell_result(33), Ok(()));
    assert_eq!(spawn_result("stub", || Ok(None)), Ok(()));
}

#[test]
fn windows_immediate_exit_is_decided_by_its_status() {
    #[cfg(unix)]
    use std::os::unix::process::ExitStatusExt;
    #[cfg(windows)]
    use std::os::windows::process::ExitStatusExt;
    assert_eq!(
        spawn_result("stub", || Ok(Some(std::process::ExitStatus::from_raw(0)))),
        Ok(())
    );
    let result = spawn_result("stub", || Ok(Some(std::process::ExitStatus::from_raw(256))));
    assert!(
        matches!(result, Err(Some(ref why)) if why.contains("stub failed") && why.contains("printed link")),
        "{result:?}"
    );
}

/// A launch plan whose program cannot exist: were the interception ever
/// removed, the start would fail with "not found" instead of opening
/// anything on the machine running the tests.
fn nowhere() -> Plan {
    let root = std::env::temp_dir().join(format!("pl-w10-no-launcher-{}", std::process::id()));
    Plan::Background {
        program: root.join("no-such-launcher").to_string_lossy().into_owned(),
        args: vec!["%s".to_owned()],
    }
}

fn refuses(plan: &Plan) -> String {
    let caught = catch_unwind(AssertUnwindSafe(|| open(plan, "http://127.0.0.1:1/ui/")))
        .expect_err("the interception let a launch through");
    caught.downcast_ref::<String>().cloned().unwrap_or_default()
}

#[test]
fn a_test_build_intercepts_every_launch_but_a_registered_stub() {
    stub_root(None);
    let message = refuses(&nowhere());
    assert!(message.contains("tests start only stubs"), "{message}");
    // A bare name (what the default launchers are) is never a stub. The
    // name cannot exist, so a guard broken here still opens nothing.
    let named = Plan::Background {
        program: "pl-w10-no-such-launcher".to_owned(),
        args: vec!["%s".to_owned()],
    };
    stub_root(Some(std::env::temp_dir()));
    refuses(&named);
    // A path that climbs out of the registered directory: refused.
    let climbing = Plan::Background {
        program: std::env::temp_dir()
            .join("..")
            .join("pl-w10-no-such-launcher")
            .to_string_lossy()
            .into_owned(),
        args: vec!["%s".to_owned()],
    };
    refuses(&climbing);
    // Outside the registered directory: refused too.
    stub_root(Some(std::env::temp_dir().join("pl-w10-some-other-root")));
    refuses(&nowhere());
    stub_root(None);
    // `ShellExecuteW` is never a stub. Checked on the guard alone, so a
    // broken guard cannot reach the real call.
    let caught = catch_unwind(|| intercept(&Plan::StartFile));
    assert!(caught.is_err(), "StartFile passed the interception");
}

#[test]
fn a_refused_plan_starts_nothing_and_keeps_its_cause() {
    stub_root(None);
    assert_eq!(open(&Plan::Refused(None), "u"), Err(None));
    assert_eq!(
        open(&Plan::Refused(Some("why; fix".to_owned())), "u"),
        Err(Some("why; fix".to_owned()))
    );
}

#[test]
fn the_url_is_one_argument_whatever_it_holds() {
    let url = "http://127.0.0.1:1/ui/#/signin?code=AB&to=board x;$(id)`id`'\"|&";
    let args = vec!["--new".to_owned(), "%s".to_owned(), "--url=%s".to_owned()];
    assert_eq!(
        arguments(&args, url),
        vec!["--new".to_owned(), url.to_owned(), format!("--url={url}")]
    );
}

#[cfg(unix)]
mod stubs {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};

    /// A fresh stub directory for one test, registered for this thread.
    struct Stubs(PathBuf);

    impl Stubs {
        fn new(name: &str) -> Self {
            let root =
                std::env::temp_dir().join(format!("pl-w10-launch-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).unwrap();
            stub_root(Some(root.clone()));
            Self(root)
        }

        /// A `/bin/sh` launcher that records its argv, one argument per
        /// NUL-terminated record, then runs `tail`.
        fn launcher(&self, tail: &str) -> String {
            let path = self.0.join("launcher");
            let record = self.0.join("argv");
            std::fs::write(
                &path,
                format!(
                    "#!/bin/sh\nfor arg in \"$@\"; do printf '%s\\0' \"$arg\" >> '{}'; done\n{tail}\n",
                    record.display()
                ),
            )
            .unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
            path.to_string_lossy().into_owned()
        }

        fn argv(&self) -> Vec<String> {
            let bytes = std::fs::read(self.0.join("argv")).unwrap_or_default();
            bytes
                .split(|byte| *byte == 0)
                .filter(|item| !item.is_empty())
                .map(|item| String::from_utf8(item.to_vec()).unwrap())
                .collect()
        }

        fn path(&self, name: &str) -> String {
            Path::new(&self.0).join(name).to_string_lossy().into_owned()
        }
    }

    impl Drop for Stubs {
        fn drop(&mut self) {
            stub_root(None);
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn plan(program: String, args: &[&str]) -> Plan {
        Plan::Background {
            program,
            args: args.iter().map(|arg| (*arg).to_owned()).collect(),
        }
    }

    #[test]
    fn a_launcher_that_exits_zero_opened_the_url_as_one_argument() {
        let stubs = Stubs::new("ok");
        let url = "http://127.0.0.1:1/ui/#/signin?code=AB&to=board x;$(id)'\"";
        let program = stubs.launcher("exit 0");
        assert_eq!(open(&plan(program, &["--new", "%s"]), url), Ok(()));
        assert_eq!(stubs.argv(), ["--new", url]);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn explorer_exit_one_is_a_handoff_but_other_failures_are_not() {
        let stubs = Stubs::new("explorer");
        let ordinary = stubs.launcher("exit 1");
        let explorer = stubs.path("explorer.exe");
        std::fs::copy(&ordinary, &explorer).unwrap();
        assert_eq!(open(&plan(explorer.clone(), &["%s"]), "http://h/"), Ok(()));
        assert!(open(&plan(ordinary.clone(), &["%s"]), "http://h/").is_err());
        std::fs::write(&explorer, "#!/bin/sh\nexit 2\n").unwrap();
        assert!(open(&plan(explorer, &["%s"]), "http://h/").is_err());
        assert_eq!(stubs.argv(), ["http://h/", "http://h/"]);
    }

    #[test]
    fn a_launcher_that_fails_reports_its_status_and_the_way_out() {
        let stubs = Stubs::new("fail");
        let program = stubs.launcher("exit 3");
        let Err(Some(cause)) = open(&plan(program.clone(), &["%s"]), "http://h/") else {
            panic!("a failing launcher reported success");
        };
        assert!(
            cause.starts_with(&format!("{program} failed (exit status: 3)")),
            "{cause}"
        );
        assert!(
            cause.ends_with(super::super::super::browser::SET_BROWSER),
            "{cause}"
        );
        assert_eq!(stubs.argv(), ["http://h/"], "started once");
    }

    #[test]
    fn a_missing_launcher_says_it_could_not_start() {
        let stubs = Stubs::new("missing");
        let program = stubs.path("absent");
        let Err(Some(cause)) = open(&plan(program.clone(), &["%s"]), "http://h/") else {
            panic!("a missing launcher reported success");
        };
        assert!(
            cause.starts_with(&format!("could not start {program} (")),
            "{cause}"
        );
        assert!(cause.contains("No such file or directory"), "{cause}");
    }

    /// A browser that stays in the foreground must not hold this command's
    /// stdout or stderr: a caller capturing them (an agent's tool, an
    /// installer) would wait for the browser to exit.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_started_program_holds_none_of_this_commands_standard_streams() {
        let stubs = Stubs::new("streams");
        let program = stubs.launcher(
            "for fd in 0 1 2; do [ \"$(readlink /proc/$$/fd/$fd)\" = /dev/null ] || exit $((10 + fd)); done",
        );
        assert_eq!(open(&plan(program, &["%s"]), "http://h/"), Ok(()));
    }

    #[test]
    fn a_launcher_still_running_after_the_settle_time_is_the_browser() {
        let stubs = Stubs::new("running");
        let program = stubs.launcher("sleep 4");
        let started = std::time::Instant::now();
        assert_eq!(open(&plan(program, &["%s"]), "http://h/"), Ok(()));
        let waited = started.elapsed();
        assert!(waited >= SETTLE && waited < SETTLE * 2, "{waited:?}");
    }

    /// A caller that ignores SIGCHLD (it survives `exec`) has the kernel
    /// reap the launcher itself, so waiting for it fails with ECHILD after
    /// a launcher that handed off and exited: that is not "no browser".
    /// Run in a child test process, so this process's own SIGCHLD stays
    /// at its default for every other test.
    #[test]
    #[allow(unsafe_code)]
    fn an_ignored_sigchld_does_not_turn_a_handed_off_launch_into_a_failure() {
        const CHILD: &str = "PL_W10_SIGCHLD_IGNORED_CHILD";
        if std::env::var_os(CHILD).is_some() {
            let stubs = Stubs::new("sigchld");
            let program = stubs.launcher("exit 0");
            assert_eq!(open(&plan(program, &["%s"]), "http://h/"), Ok(()));
            return;
        }
        use std::os::unix::process::CommandExt;
        let mut command = std::process::Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "cli::console::launch::tests::stubs::an_ignored_sigchld_does_not_turn_a_handed_off_launch_into_a_failure",
                "--test-threads=1",
            ])
            .env(CHILD, "1");
        // SAFETY: `signal` is async-signal-safe; the closure allocates
        // nothing and only sets the child's SIGCHLD disposition.
        unsafe {
            command.pre_exec(|| {
                libc::signal(libc::SIGCHLD, libc::SIG_IGN);
                Ok(())
            })
        };
        let output = command.output().unwrap();
        let text = String::from_utf8_lossy(&output.stdout);
        assert!(output.status.success(), "{text}");
        assert!(text.contains("1 passed"), "the child ran no test: {text}");
    }
}
