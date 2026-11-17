//! The browser choice against a fake machine: nothing here starts a
//! program or reads the real environment.
use super::*;
use std::collections::BTreeMap;

#[derive(Default)]
struct Fake {
    vars: BTreeMap<&'static str, OsString>,
    found: Vec<&'static str>,
}

impl Fake {
    fn with(mut self, key: &'static str, value: &str) -> Self {
        self.vars.insert(key, value.into());
        self
    }

    fn found(mut self, names: &[&'static str]) -> Self {
        self.found.extend(names);
        self
    }
}

impl Host for Fake {
    fn var(&self, key: &str) -> Option<OsString> {
        self.vars.get(key).cloned()
    }

    fn which(&self, name: &str) -> bool {
        self.found.contains(&name)
    }
}

fn background(program: &str, args: &[&str]) -> Plan {
    Plan::Background {
        program: program.to_owned(),
        args: args.iter().map(|arg| (*arg).to_owned()).collect(),
    }
}

/// The cause a refused POSIX plan prints, which every one of them has.
fn cause(plan: Plan) -> String {
    match plan {
        Plan::Refused(Some(cause)) => {
            assert!(cause.contains(SET_BROWSER), "{cause}");
            cause
        }
        other => panic!("not a refusal with a cause: {other:?}"),
    }
}

use Platform::{Mac, Posix, Windows};

#[test]
fn mac_ssh_prints_the_link_unless_browser_is_explicit() {
    for key in ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"] {
        let remote = Fake::default().with(key, "remote");
        let Plan::Refused(Some(reason)) = choose(&remote, Mac) else {
            panic!("SSH should print the link")
        };
        assert!(reason.contains("SSH"));
        assert!(reason.contains("machine in front of you"));
        assert!(!reason.contains("firefox"));
        assert_eq!(
            choose(&remote.with("BROWSER", "stub %s"), Mac),
            background("stub", &["%s"])
        );
        assert_eq!(
            choose(&Fake::default().with(key, ""), Mac),
            background(MAC_OPEN, &["%s"])
        );
    }
}

#[test]
fn windows_refusals_explain_unquoted_spaces() {
    for (value, cause, fix) in [
        (
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            "unquoted",
            "quote the executable path",
        ),
        (
            r"C:\Program Files\browser.exe %s &",
            "unquoted",
            "quote the executable path",
        ),
    ] {
        let plan = choose(&Fake::default().with("BROWSER", value), Windows);
        assert!(
            matches!(plan, Plan::Refused(Some(ref why)) if why.contains(cause) && why.contains(fix)),
            "{value}: {plan:?}"
        );
    }
}

#[test]
fn windows_refusals_explain_network_and_device_paths() {
    for (value, cause, fix) in [
        (r"\\server\share\browser.exe", "UNC", "drive-letter path"),
        (r"\\?\C:\browser.exe", "device path", "drive-letter path"),
    ] {
        let plan = choose(&Fake::default().with("BROWSER", value), Windows);
        assert!(
            matches!(plan, Plan::Refused(Some(ref why)) if why.contains(cause) && why.contains(fix)),
            "{value}: {plan:?}"
        );
    }
}

#[test]
fn windows_refuses_command_shells() {
    for (value, cause, fix) in [
        (
            r"C:\Windows\System32\cmd.exe /c start %s",
            "command shell",
            "browser executable directly",
        ),
        (
            r"C:\Tools\PowerShell.exe %s",
            "command shell",
            "browser executable directly",
        ),
        (
            r"C:\Tools\pwsh.exe %s",
            "command shell",
            "browser executable directly",
        ),
    ] {
        let plan = choose(&Fake::default().with("BROWSER", value), Windows);
        assert!(
            matches!(plan, Plan::Refused(Some(ref why)) if why.contains(cause) && why.contains(fix)),
            "{value}: {plan:?}"
        );
    }
}

#[test]
fn windows_accepts_ordinary_paths_and_names_every_refusal() {
    for value in [
        r"C:\Tools\browser.exe %s &",
        r"C:\Tools\browser.exe",
        "C:/Tools/browser.exe %s",
    ] {
        let plan = choose(&Fake::default().with("BROWSER", value), Windows);
        assert!(matches!(plan, Plan::Background { .. }), "{value}: {plan:?}");
    }
    for value in ["firefox", "b.cmd %s &", "b.exe %s &", " ", "'unclosed"] {
        let plan = choose(&Fake::default().with("BROWSER", value), Windows);
        assert!(
            matches!(plan, Plan::Refused(Some(ref why)) if why.contains("BROWSER") && why.contains("printed link")),
            "{value}: {plan:?}"
        );
    }
}

/// `[text, words or null]` from CPython 3.11.9's `shlex.split`, 2026-11-13.
const SHLEX: &str = include_str!("shlex_cases.json");

#[test]
fn shlex_splits_as_cpython_does() {
    let super::super::pyjson::J::List(cases) = super::super::pyjson::loads(SHLEX, 10).unwrap()
    else {
        panic!("not a list");
    };
    assert!(cases.len() > 20);
    for case in cases {
        use super::super::pyjson::J;
        let J::List(case) = case else { panic!() };
        let J::Str(text) = &case[0] else { panic!() };
        let want = match &case[1] {
            J::Null => Err(()),
            J::List(words) => Ok(words
                .iter()
                .map(|word| word.as_str().unwrap().to_owned())
                .collect::<Vec<_>>()),
            other => panic!("{other:?}"),
        };
        assert_eq!(shlex_split(text), want, "{text:?}");
    }
}

#[test]
fn a_posix_browser_variable_is_a_command_line_that_starts_in_the_background() {
    for platform in [Posix, Mac] {
        let posix = |value: &str| choose(&Fake::default().with("BROWSER", value), platform);
        assert_eq!(
            posix("'/opt/stand in/b' --new %s &"),
            background("/opt/stand in/b", &["--new", "%s"])
        );
        // The first non-empty entry decides; the rest are never read.
        assert_eq!(posix("/x/b %s &:/y/c %s &"), background("/x/b", &["%s"]));
        assert_eq!(posix(":/x/b %s &"), background("/x/b", &["%s"]));
        assert_eq!(posix("/x/b %s &: "), background("/x/b", &["%s"]));
        assert_eq!(posix("b --url=%s '&'"), background("b", &["--url=%s"]));
        assert_eq!(posix("b %s &"), background("b", &["%s"]));
        // No trailing `&`: still a background start in its own session.
        assert_eq!(posix("/x/b %s"), background("/x/b", &["%s"]));
        assert_eq!(posix("/x/b %s&"), background("/x/b", &["%s&"]));
        // Without `%s` the URL is the last argument.
        assert_eq!(posix("firefox"), background("firefox", &["%s"]));
        assert_eq!(
            posix("/usr/bin/firefox"),
            background("/usr/bin/firefox", &["%s"])
        );
        assert_eq!(
            posix("open -a Safari"),
            background("open", &["-a", "Safari", "%s"])
        );
        assert_eq!(
            posix("/x/my-browser --flag"),
            background("/x/my-browser", &["--flag", "%s"])
        );
        // Each refusal names its cause and the way out.
        assert!(cause(posix("'/x/b %s &")).contains("unclosed quote"));
        assert!(cause(posix(" ")).contains("names no program"));
        assert!(cause(posix("Lynx -dump")).contains("terminal browser lynx"));
        assert!(cause(posix("/usr/bin/w3m %s")).contains("terminal browser w3m"));
        assert!(cause(posix("&")).contains("names no program"));
    }
}

#[test]
fn windows_runs_only_an_explicit_exe_and_preserves_quoted_paths() {
    let windows = |value: &str| choose(&Fake::default().with("BROWSER", value), Windows);
    assert_eq!(
        windows("'C:\\Program Files\\B\\b.exe' %s &"),
        background("C:\\Program Files\\B\\b.exe", &["%s"])
    );
    assert_eq!(
        windows("C:/b/b.EXE %s &"),
        background("C:/b/b.EXE", &["%s"])
    );
    // `;` separates entries on Windows, `:` does not.
    assert_eq!(
        windows("C:/b/b.exe %s &;D:/c.exe %s &"),
        background("C:/b/b.exe", &["%s"])
    );
    for value in [
        "b.exe %s &",
        "bin\\b.exe %s &",
        "'C:\\b\\b.cmd' %s &",
        "'C:\\b\\b.bat' %s &",
        "'C:\\b\\b' %s &",
        "'\\\\server\\share\\b.exe' %s &",
        "'C:\\b\\\"b.exe' %s &",
        "firefox",
    ] {
        assert!(matches!(windows(value), Plan::Refused(Some(_))), "{value}");
    }
    for value in ["1:firefox", "é:Firefox --private"] {
        assert!(matches!(windows(value), Plan::Refused(Some(_))));
    }
    assert_eq!(windows(r"C:\b\my.exe"), background(r"C:\b\my.exe", &["%s"]));
    assert_eq!(windows("C:/b/b.exe %s"), background("C:/b/b.exe", &["%s"]));
    assert_eq!(
        windows("C:/b/b.exe %s &; "),
        background("C:/b/b.exe", &["%s"])
    );
}

#[test]
fn without_a_browser_variable_the_platform_default_decides() {
    assert_eq!(choose(&Fake::default(), Windows), Plan::StartFile);
    assert_eq!(
        choose(&Fake::default().with("BROWSER", ""), Windows),
        Plan::StartFile
    );
    assert_eq!(
        choose(&Fake::default().with("BROWSER", ";;"), Windows),
        Plan::StartFile
    );
    // Windows reads none of the POSIX launchers.
    assert_eq!(
        choose(&Fake::default().found(&["xdg-open"]), Windows),
        Plan::StartFile
    );
    // macOS: an empty BROWSER is no BROWSER.
    for value in ["", "::"] {
        assert_eq!(
            choose(&Fake::default().with("BROWSER", value), Mac),
            background(MAC_OPEN, &["%s"])
        );
    }
    // macOS never searches PATH for its launcher.
    assert_eq!(
        choose(&Fake::default().found(&["xdg-open"]), Mac),
        background(MAC_OPEN, &["%s"])
    );
    let x = || Fake::default().with("DISPLAY", ":0");
    // Nothing found: the link, the names looked for and the way out.
    let none = cause(choose(&x(), Posix));
    assert!(none.contains("no browser launcher found on PATH"), "{none}");
    for (name, _) in POSIX_LAUNCHERS {
        assert!(none.contains(name), "{none}");
    }
    // The order, each step against everything after it.
    let names: Vec<&'static str> = POSIX_LAUNCHERS.iter().map(|(name, _)| *name).collect();
    assert_eq!(
        names,
        [
            "xdg-open",
            "gio",
            "x-www-browser",
            "firefox",
            "google-chrome",
            "chromium",
            "chromium-browser"
        ]
    );
    for (index, (name, args)) in POSIX_LAUNCHERS.iter().enumerate() {
        let found: Vec<&'static str> = names[index..].to_vec();
        assert_eq!(
            choose(&x().found(&found), Posix),
            background(name, args),
            "{name}"
        );
    }
    assert_eq!(
        choose(&x().found(&["gio"]), Posix),
        background("gio", &["open", "--", "%s"])
    );
    // A set BROWSER decides alone, even when it cannot start.
    assert_eq!(
        choose(
            &x().with("BROWSER", "no-such-browser").found(&["xdg-open"]),
            Posix
        ),
        background("no-such-browser", &["%s"])
    );
    assert!(
        cause(choose(
            &x().with("BROWSER", "lynx").found(&["xdg-open"]),
            Posix
        ))
        .contains("terminal browser")
    );
    assert_eq!(
        choose(&Fake::default().with("BROWSER", "no-such-browser"), Mac),
        background("no-such-browser", &["%s"])
    );
    // The old console browsers and launchers are no longer looked up.
    assert!(
        cause(choose(
            &x().found(&["lynx", "w3m", "mosaic", "skipstone"]),
            Posix
        ))
        .contains("no browser launcher found")
    );
}

#[cfg(unix)]
#[test]
fn a_browser_variable_that_is_not_utf8_prints_the_link_with_its_cause() {
    use std::os::unix::ffi::OsStringExt;
    let mut fake = Fake::default();
    fake.vars
        .insert("BROWSER", OsString::from_vec(b"/x/\xff %s &".to_vec()));
    assert!(cause(choose(&fake, Posix)).contains("not valid UTF-8"));
}

#[cfg(windows)]
#[test]
fn a_browser_variable_that_is_not_utf16_has_a_cause() {
    use std::os::windows::ffi::OsStringExt;
    let mut fake = Fake::default();
    fake.vars
        .insert("BROWSER", OsString::from_wide(&[0x43, 0xd800, 0x25, 0x73]));
    assert!(
        matches!(choose(&fake, Windows), Plan::Refused(Some(why)) if why.contains("not valid Unicode"))
    );
}

#[cfg(unix)]
#[test]
fn programs_are_looked_up_as_cpython_looks_them_up() {
    use super::super::launch::{candidates, which_in};
    let paths = |program: &str, path: Option<&str>| -> Vec<String> {
        candidates(program.as_bytes(), path.map(OsString::from))
            .into_iter()
            .map(|path| String::from_utf8(path).unwrap())
            .collect()
    };
    assert_eq!(paths("/x/b", Some("/a")), ["/x/b"]);
    assert_eq!(paths("x/b", Some("/a")), ["x/b"]);
    assert_eq!(paths("b", Some("/a:/c/::")), ["/a/b", "/c/b", "b", "b"]);
    assert_eq!(paths("b", Some("")), ["b"]);
    assert_eq!(paths("b", None), ["/bin/b", "/usr/bin/b"]);
    // shutil.which: an executable file, never a directory or a plain file.
    let root = std::env::temp_dir().join(format!("console-which-{}", std::process::id()));
    std::fs::create_dir_all(root.join("dir/b")).unwrap();
    std::fs::write(root.join("plain"), b"").unwrap();
    let tool = root.join("tool");
    std::fs::write(&tool, b"").unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&tool, std::fs::Permissions::from_mode(0o755)).unwrap();
    let dir = root.to_str().unwrap();
    assert!(which_in("tool", Some(dir.into())));
    assert!(!which_in("plain", Some(dir.into())));
    assert!(!which_in("b", Some(format!("{dir}/dir").into())));
    assert!(!which_in("tool", Some("".into())));
    std::fs::remove_dir_all(&root).unwrap();
}

#[test]
fn an_ordinary_desktop_gets_a_launch_plan_without_a_browser_variable() {
    // macOS: the system `open`, by absolute path, so PATH cannot swap it.
    assert_eq!(
        choose(&Fake::default(), Mac),
        background("/usr/bin/open", &["%s"])
    );
    // Linux desktop whose default browser is Firefox: xdg-open honours that
    // default itself, so it stays the launcher. (Watched red at 702180256
    // with `xdg-settings` answering `firefox.desktop`: that deferred.)
    let firefox_default = Fake::default()
        .with("DISPLAY", ":0")
        .found(&["xdg-open", "firefox"]);
    assert_eq!(
        choose(&firefox_default, Posix),
        background("xdg-open", &["%s"])
    );
    // A Wayland desktop with only a browser installed: that browser.
    let chrome_only = Fake::default()
        .with("WAYLAND_DISPLAY", "wayland-0")
        .found(&["google-chrome"]);
    assert_eq!(
        choose(&chrome_only, Posix),
        background("google-chrome", &["%s"])
    );
}

#[test]
fn a_set_browser_decides_without_a_display_and_none_says_why() {
    // A remote terminal (VS Code, Codespaces, WSL's wslview) has no display
    // but names its own opener in BROWSER: that opener is what works.
    let remote = Fake::default()
        .with("BROWSER", "/home/u/.vscode-server/bin/helpers/browser.sh")
        .found(&["xdg-open"]);
    assert_eq!(
        choose(&remote, Posix),
        background("/home/u/.vscode-server/bin/helpers/browser.sh", &["%s"])
    );
    // Without BROWSER the desktop launchers need a display.
    let headless = cause(choose(&Fake::default().found(&["xdg-open"]), Posix));
    assert!(headless.starts_with(NO_DISPLAY), "{headless}");
    for key in ["DISPLAY", "WAYLAND_DISPLAY"] {
        let empty = Fake::default().with(key, "").found(&["xdg-open"]);
        assert!(cause(choose(&empty, Posix)).starts_with(NO_DISPLAY));
        let set = Fake::default().with(key, ":0").found(&["xdg-open"]);
        assert_eq!(choose(&set, Posix), background("xdg-open", &["%s"]));
    }
    // macOS and Windows never read DISPLAY.
    assert_eq!(choose(&Fake::default(), Mac), background(MAC_OPEN, &["%s"]));
    assert_eq!(choose(&Fake::default(), Windows), Plan::StartFile);
}
