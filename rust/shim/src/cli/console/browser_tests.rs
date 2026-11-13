//! The browser choice against a fake machine: nothing here starts a
//! program or reads the real environment.
use super::*;
use std::collections::BTreeMap;

#[derive(Default)]
struct Fake {
    vars: BTreeMap<&'static str, OsString>,
    found: Vec<&'static str>,
    probe: Option<Probe>,
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

    fn probe(mut self, probe: Probe) -> Self {
        self.probe = Some(probe);
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

    fn probe(&self) -> Probe {
        self.probe.clone().unwrap_or(Probe::Missing)
    }
}

fn background(program: &str, args: &[&str]) -> Result<Plan, Unsupported> {
    Ok(Plan::Background {
        program: program.to_owned(),
        args: args.iter().map(|arg| (*arg).to_owned()).collect(),
    })
}

use Platform::{Mac, Posix, Windows};

#[test]
fn a_display_is_needed_only_on_other_posix_systems() {
    let none = Fake::default();
    assert!(has_display(&none, Windows));
    assert!(has_display(&none, Mac));
    assert!(!has_display(&none, Posix));
    assert!(!has_display(&Fake::default().with("DISPLAY", ""), Posix));
    assert!(has_display(&Fake::default().with("DISPLAY", ":0"), Posix));
    assert!(has_display(
        &Fake::default().with("WAYLAND_DISPLAY", "wayland-0"),
        Posix
    ));
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
fn a_browser_command_with_a_trailing_ampersand_runs_in_the_background() {
    let posix = |value: &str| choose(&Fake::default().with("BROWSER", value), Posix);
    assert_eq!(
        posix("'/opt/stand in/b' --new %s &"),
        background("/opt/stand in/b", &["--new", "%s"])
    );
    // The first entry decides; the rest are only registered.
    assert_eq!(posix("/x/b %s &:/y/c %s &"), background("/x/b", &["%s"]));
    assert_eq!(posix(":/x/b %s &"), background("/x/b", &["%s"]));
    assert_eq!(posix("b --url=%s '&'"), background("b", &["--url=%s"]));
    // A bare name runs through CPython's own PATH search, reproduced.
    assert_eq!(posix("b %s &"), background("b", &["%s"]));
    // No trailing `&`: a GenericBrowser, which open_browser refuses.
    assert_eq!(posix("/x/b %s"), Ok(Plan::Refused));
    assert_eq!(posix("/x/b %s&"), Ok(Plan::Refused));
    // shlex.split raises: webbrowser.get() raises.
    assert_eq!(posix("'/x/b %s &"), Ok(Plan::Refused));
    // An entry that is only whitespace raises IndexError in _synthesize.
    assert_eq!(posix("/x/b %s &: "), Ok(Plan::Refused));
    assert_eq!(posix("\u{3000}:/x/b %s &"), Ok(Plan::Refused));
    // Not `%s`: a GenericBrowser, unless _synthesize may clone a standard one.
    assert_eq!(posix("/x/my-browser --flag"), Ok(Plan::Refused));
    assert_eq!(posix("/usr/bin/firefox"), Err(Unsupported));
    assert_eq!(posix("Lynx -dump"), Err(Unsupported));
    assert_eq!(posix("\u{212a}fm"), Err(Unsupported));
}

#[test]
fn windows_runs_only_an_absolute_exe_it_starts_the_same_way() {
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
        // shlex eats the backslashes.
        "C:\\b\\b.exe %s &",
        "b.exe %s &",
        "bin\\b.exe %s &",
        "'C:\\b\\b.cmd' %s &",
        "'C:\\b\\b.bat' %s &",
        "'C:\\b\\b' %s &",
        "'\\\\server\\share\\b.exe' %s &",
        "'C:\\b\\\"b.exe' %s &",
        "firefox",
    ] {
        assert_eq!(windows(value), Err(Unsupported), "{value}");
    }
    // `ntpath.splitdrive` takes whatever precedes a colon in second place
    // as the drive, a digit or a non-ASCII letter included.
    assert_eq!(windows("1:firefox"), Err(Unsupported));
    assert_eq!(windows("\u{e9}:Firefox --private"), Err(Unsupported));
    // A clone needs the basename itself to be a registered name.
    assert_eq!(windows("C:\\b\\my.exe"), Ok(Plan::Refused));
    assert_eq!(windows("C:\\b\\chrome.exe"), Ok(Plan::Refused));
    assert_eq!(windows("C:/b/b.exe %s"), Ok(Plan::Refused));
}

#[test]
fn without_a_browser_variable_the_platform_default_decides() {
    assert_eq!(choose(&Fake::default(), Windows), Ok(Plan::StartFile));
    assert_eq!(
        choose(&Fake::default().with("BROWSER", ""), Windows),
        Ok(Plan::StartFile)
    );
    assert_eq!(
        choose(&Fake::default().with("BROWSER", ";;"), Windows),
        Ok(Plan::StartFile)
    );
    // macOS: the AppleScript launcher is not ported.
    assert_eq!(choose(&Fake::default(), Mac), Err(Unsupported));
    let x = || Fake::default().with("DISPLAY", ":0");
    // Nothing found: webbrowser.Error, so no browser.
    assert_eq!(choose(&x(), Posix), Ok(Plan::Refused));
    assert_eq!(
        choose(&x().found(&["xdg-open", "firefox"]), Posix),
        background("xdg-open", &["%s"])
    );
    assert_eq!(
        choose(&x().found(&["gio", "x-www-browser"]), Posix),
        background("gio", &["open", "--", "%s"])
    );
    // gvfs-open only in a GNOME session.
    assert_eq!(
        choose(&x().found(&["gvfs-open", "mosaic"]), Posix),
        background("mosaic", &["%s"])
    );
    assert_eq!(
        choose(
            &x().with("GNOME_DESKTOP_SESSION_ID", "")
                .found(&["gvfs-open", "mosaic"]),
            Posix
        ),
        background("gvfs-open", &["%s"])
    );
    // xdg-settings' answer moves a matching name to the front.
    let preferred = x()
        .found(&["xdg-open", "firefox"])
        .probe(Probe::Answer("firefox.desktop".to_owned()));
    assert_eq!(choose(&preferred, Posix), Err(Unsupported));
    let preferred = x()
        .found(&["xdg-open", "skipstone"])
        .probe(Probe::Answer("skipstone.desktop".to_owned()));
    assert_eq!(choose(&preferred, Posix), background("skipstone", &["%s"]));
    // A probe that raises makes webbrowser.get() raise, BROWSER or not.
    let raised = x()
        .with("BROWSER", "/x/b %s &")
        .found(&["xdg-open"])
        .probe(Probe::Raised);
    assert_eq!(choose(&raised, Posix), Ok(Plan::Refused));
    // Mozilla, Chrome and the other UnixBrowser launchers are not ported.
    assert_eq!(
        choose(
            &x().found(&["firefox", "xdg-open"]).probe(Probe::Missing),
            Posix
        ),
        background("xdg-open", &["%s"])
    );
    assert_eq!(
        choose(&x().found(&["google-chrome"]), Posix),
        Err(Unsupported)
    );
    // Console browsers, with TERM: every one is refused, elinks (a
    // UnixBrowser that falls back to the terminal) by name.
    let term = || Fake::default().with("TERM", "xterm");
    assert_eq!(
        choose(&term().found(&["lynx", "w3m"]), Posix),
        Ok(Plan::Refused)
    );
    assert_eq!(choose(&term().found(&["elinks"]), Posix), Ok(Plan::Refused));
    assert_eq!(
        choose(&term().found(&["elinks", "lynx"]), Posix),
        Ok(Plan::Refused)
    );
    // With no display the X list is never registered.
    assert_eq!(
        choose(&term().found(&["xdg-open"]), Posix),
        Ok(Plan::Refused)
    );
}

#[cfg(unix)]
#[test]
fn a_browser_variable_that_is_not_utf8_defers() {
    use std::os::unix::ffi::OsStringExt;
    let mut fake = Fake::default();
    fake.vars
        .insert("BROWSER", OsString::from_vec(b"/x/\xff %s &".to_vec()));
    assert_eq!(choose(&fake, Posix), Err(Unsupported));
}

#[cfg(windows)]
#[test]
fn a_browser_variable_that_is_not_utf16_defers() {
    use std::os::windows::ffi::OsStringExt;
    let mut fake = Fake::default();
    fake.vars
        .insert("BROWSER", OsString::from_wide(&[0x43, 0xd800, 0x25, 0x73]));
    assert_eq!(choose(&fake, Windows), Err(Unsupported));
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
