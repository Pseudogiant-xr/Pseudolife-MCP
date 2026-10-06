use pseudolife_stdio::lifecycle;
use serde::{Deserialize, Serialize};
use std::{
    ffi::{OsStr, OsString},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-executable-test-{}",
            uuid::Uuid::new_v4()
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn executable(&self, name: impl AsRef<Path>) -> PathBuf {
        let path = self.0.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, b"disposable executable fixture").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        path
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[derive(Serialize, Deserialize)]
struct Case {
    program: String,
    expected: Option<PathBuf>,
}
fn case(program: impl AsRef<OsStr>, expected: Option<PathBuf>) -> Case {
    Case {
        program: program.as_ref().to_str().unwrap().to_owned(),
        expected,
    }
}
#[derive(Serialize, Deserialize)]
struct Cell {
    cases: Vec<Case>,
    pathext_after_first_lookup: Option<String>,
    launcher_expected: Option<String>,
}

#[test]
fn resolver_fixture_child() {
    let Some(input) = std::env::var_os("RESOLVER_FIXTURE_INPUT") else {
        return;
    };
    let input = PathBuf::from(input);
    let cell: Cell = serde_json::from_slice(&fs::read(&input).unwrap()).unwrap();
    let mut matches = Vec::new();
    for (index, case) in cell.cases.iter().enumerate() {
        matches.push(lifecycle::find_executable(&case.program).ok() == case.expected);
        #[cfg(windows)]
        if index == 0
            && let Some(value) = &cell.pathext_after_first_lookup
        {
            #[allow(unsafe_code)]
            // SAFETY: Windows environment mutation is thread-safe; this is a disposable child.
            unsafe {
                std::env::set_var("PATHEXT", value);
            }
        }
        #[cfg(not(windows))]
        let _ = index;
    }
    if let Some(expected) = cell.launcher_expected {
        matches.push(lifecycle::launcher_command() == expected);
    }
    fs::write(
        input.with_extension("results"),
        serde_json::to_vec(&matches).unwrap(),
    )
    .unwrap();
}

fn run(
    home: &Home,
    path: Option<OsString>,
    pathext: Option<OsString>,
    cell: Cell,
    launcher: Option<&Path>,
) {
    let input = home.0.join("input.json");
    fs::write(&input, serde_json::to_vec(&cell).unwrap()).unwrap();
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args([
            "--exact",
            "resolver_fixture_child",
            "--nocapture",
            "--test-threads=1",
        ])
        .env_clear()
        .env("RESOLVER_FIXTURE_INPUT", &input)
        .env("HOME", &home.0)
        .env("USERPROFILE", &home.0)
        .current_dir(&home.0);
    if let Some(path) = path {
        command.env("PATH", path);
    }
    if let Some(pathext) = pathext {
        command.env("PATHEXT", pathext);
    }
    if let Some(launcher) = launcher {
        command
            .env("PSEUDOLIFE_SHIM_LAUNCHER", launcher)
            .env("PSEUDOLIFE_SHIM_RUNTIMES", home.0.join("runtimes"));
    }
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "resolver fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let matches: Vec<bool> =
        serde_json::from_slice(&fs::read(input.with_extension("results")).unwrap()).unwrap();
    assert_eq!(
        matches.len(),
        cell.cases.len() + usize::from(launcher.is_some())
    );
    assert!(
        matches.iter().all(|matched| *matched),
        "resolution matrix: {matches:?}"
    );
}
fn cell(cases: Vec<Case>) -> Cell {
    Cell {
        cases,
        pathext_after_first_lookup: None,
        launcher_expected: None,
    }
}
fn name() -> &'static str {
    if cfg!(windows) {
        "runner.CMD"
    } else {
        "runner"
    }
}
fn extensions() -> Option<OsString> {
    Some(".CMD;.EXE".into())
}

#[test]
fn path_order_relative_entries_and_explicit_paths() {
    let home = Home::new();
    let first = home.executable(Path::new("first").join(name()));
    let second = home.executable(Path::new("second").join(name()));
    fs::create_dir(home.0.join("directory")).unwrap();
    run(
        &home,
        Some(std::env::join_paths([Path::new("second"), Path::new("first")]).unwrap()),
        extensions(),
        cell(vec![
            case("runner", Some(second)),
            case(&first, Some(first.clone())),
            case(Path::new(".").join("first").join(name()), Some(first)),
            case(home.0.join("directory"), None),
            case("missing", None),
        ]),
        None,
    );
}

#[test]
fn missing_path_still_accepts_explicit_paths() {
    let home = Home::new();
    let selected = home.executable(name());
    run(
        &home,
        None,
        extensions(),
        cell(vec![
            case(&selected, Some(selected.clone())),
            case("runner", None),
        ]),
        None,
    );
}

#[test]
fn empty_path_entries_retain_the_platform_current_directory_policy() {
    let home = Home::new();
    let current = home.executable(name());
    let selected = home.executable(Path::new("bin").join(name()));
    run(
        &home,
        Some(std::env::join_paths([Path::new(""), Path::new("bin")]).unwrap()),
        extensions(),
        cell(vec![case(
            "runner",
            Some(if cfg!(windows) { selected } else { current }),
        )]),
        None,
    );
}

#[test]
fn tilde_expands_only_a_standalone_home_component() {
    let home = Home::new();
    let selected = home.executable(Path::new("bin").join(name()));
    run(
        &home,
        Some(std::env::join_paths([Path::new("~/bin")]).unwrap()),
        extensions(),
        cell(vec![case("runner", Some(selected))]),
        None,
    );
    let literal = home.executable(Path::new("~other").join(name()));
    run(
        &home,
        Some(std::env::join_paths([Path::new("~other")]).unwrap()),
        extensions(),
        cell(vec![case("runner", Some(literal))]),
        None,
    );
}

#[test]
fn launcher_lookup_keeps_canonical_identity_and_quoting() {
    let home = Home::new();
    let filename = if cfg!(windows) {
        "pseudolife-mcp.exe"
    } else {
        "pseudolife-mcp"
    };
    let launcher = home.executable(Path::new("bin with space").join(filename));
    let mut identity = cell(vec![]);
    identity.launcher_expected = Some("pseudolife-mcp".into());
    run(
        &home,
        Some(std::env::join_paths([launcher.parent().unwrap()]).unwrap()),
        extensions(),
        identity,
        Some(&launcher),
    );
    let mut quoted = cell(vec![]);
    quoted.launcher_expected = Some(format!("\"{}\"", launcher.display()));
    run(&home, None, extensions(), quoted, Some(&launcher));
}

#[cfg(unix)]
#[test]
fn unix_uses_real_id_access_and_follows_regular_file_symlinks() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let home = Home::new();
    let executable = home.executable("executable");
    let denied = home.executable("denied");
    fs::set_permissions(&denied, fs::Permissions::from_mode(0o600)).unwrap();
    let link = home.0.join("link");
    symlink(&executable, &link).unwrap();
    let denied_link = home.0.join("denied-link");
    symlink(&denied, &denied_link).unwrap();
    let broken = home.0.join("broken");
    symlink(home.0.join("missing"), &broken).unwrap();
    assert_eq!(lifecycle::find_executable(&link).unwrap(), link);
    for rejected in [&denied, &denied_link, &broken, &home.0] {
        assert!(lifecycle::find_executable(rejected).is_err());
    }
    // An execute bit for another class must not override the owner's denied access.
    if !rustix::process::getuid().is_root() {
        fs::set_permissions(&denied, fs::Permissions::from_mode(0o001)).unwrap();
        assert!(lifecycle::find_executable(&denied).is_err());
    }
}

#[cfg(unix)]
#[test]
fn non_unicode_path_fixture_child() {
    let Some(expected) = std::env::var_os("EXPECTED_EXECUTABLE") else {
        return;
    };
    assert_eq!(
        lifecycle::find_executable("runner").unwrap(),
        PathBuf::from(expected)
    );
}

#[cfg(unix)]
#[test]
fn unix_non_unicode_names_and_path_entries_remain_os_strings() {
    use std::os::unix::ffi::OsStringExt;
    let home = Home::new();
    let directory = PathBuf::from(OsString::from_vec(b"bin-\xff".to_vec()));
    let selected = home.executable(directory.join("runner"));
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "non_unicode_path_fixture_child",
            "--nocapture",
            "--test-threads=1",
        ])
        .env_clear()
        .env(
            "PATH",
            std::env::join_paths([home.0.join(directory)]).unwrap(),
        )
        .env("EXPECTED_EXECUTABLE", &selected)
        .current_dir(&home.0)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "non-Unicode PATH fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(lifecycle::find_executable(&selected).unwrap(), selected);
    let selected = home.executable(OsString::from_vec(b"runner-\xff".to_vec()));
    assert_eq!(lifecycle::find_executable(&selected).unwrap(), selected);
}

#[cfg(windows)]
#[test]
fn windows_pathext_order_raw_candidates_known_extensions_and_casing() {
    let home = Home::new();
    let cmd = home.executable("Tool.CmD");
    let exe = home.executable("Tool.ExE");
    let raw = home.executable("raw.odd");
    home.executable("raw.odd.CMD");
    let appended = home.executable("extra.odd.CMD");
    home.executable("known.CMD.CMD");
    run(
        &home,
        Some(home.0.clone().into_os_string()),
        Some("bad;;.EXE;.CMD".into()),
        cell(vec![
            case("tool", Some(exe)),
            case("tool.cmd", Some(cmd)),
            case("raw.odd", Some(raw)),
            case("extra.odd", Some(appended)),
            case("known.CMD", None),
        ]),
        None,
    );
}

#[cfg(windows)]
#[test]
fn windows_extensionless_binary_validation_precedes_suffixes() {
    let home = Home::new();
    let binary = home.0.join("native");
    fs::copy(std::env::current_exe().unwrap(), &binary).unwrap();
    home.executable("native.CMD");
    home.executable("text");
    let cmd = home.executable("text.CMD");
    run(
        &home,
        Some(home.0.clone().into_os_string()),
        extensions(),
        cell(vec![
            case("native", Some(binary.clone())),
            case("text", Some(cmd)),
        ]),
        None,
    );
    run(
        &home,
        Some(home.0.clone().into_os_string()),
        None,
        cell(vec![case("native", Some(binary)), case("text", None)]),
        None,
    );
}

#[cfg(windows)]
#[test]
fn windows_pathext_changes_between_calls_and_non_unicode_values_are_empty() {
    use std::os::windows::ffi::OsStringExt;
    let home = Home::new();
    let first = home.executable("first.CMD");
    home.executable("second.CMD");
    let mut changed = cell(vec![case("first", Some(first)), case("second", None)]);
    changed.pathext_after_first_lookup = Some(".EXE".into());
    run(
        &home,
        Some(home.0.clone().into_os_string()),
        Some(".CMD".into()),
        changed,
        None,
    );
    run(
        &home,
        Some(home.0.clone().into_os_string()),
        Some(OsString::from_wide(&[0xd800])),
        cell(vec![case("first", None)]),
        None,
    );
}

#[cfg(windows)]
#[test]
fn windows_drive_and_root_relative_path_entries_keep_native_spelling() {
    let home = Home::new();
    home.executable(Path::new("bin").join(name()));
    let drive = home
        .0
        .components()
        .next()
        .unwrap()
        .as_os_str()
        .to_str()
        .unwrap();
    let entry = PathBuf::from(format!("{drive}bin"));
    run(
        &home,
        Some(entry.clone().into_os_string()),
        extensions(),
        cell(vec![case("runner", Some(entry.join(name())))]),
        None,
    );
    let mut components = home.0.components();
    components.next();
    let entry = components.as_path().join("bin");
    run(
        &home,
        Some(entry.clone().into_os_string()),
        extensions(),
        cell(vec![case("runner", Some(entry.join(name())))]),
        None,
    );
}
