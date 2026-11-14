use super::*;

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("pl-cw-src-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn program(dir: &Path) -> PathBuf {
    let path = dir.join(if cfg!(windows) { "gh.exe" } else { "gh" });
    std::fs::write(&path, b"x").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    path
}

#[test]
fn gh_is_found_only_in_absolute_path_directories() {
    let scratch = Scratch::new();
    let found = program(&scratch.0);
    let path = |entries: &[&std::ffi::OsStr]| Some(std::env::join_paths(entries).unwrap());
    assert_eq!(find_program("gh", path(&[scratch.0.as_os_str()]), None), Some(found));
    // Relative and empty entries name the current directory: never searched.
    for entries in [&["."][..], &[""], &["sub"], &["./bin"]] {
        let entries: Vec<&std::ffi::OsStr> = entries.iter().map(std::ffi::OsStr::new).collect();
        assert_eq!(find_program("gh", path(&entries), None), None, "{entries:?}");
    }
    assert_eq!(find_program("gh", None, None), None);
}

#[cfg(windows)]
#[test]
fn only_extensions_windows_starts_are_tried() {
    let scratch = Scratch::new();
    std::fs::write(scratch.0.join("gh.ps1"), b"x").unwrap();
    let path = Some(scratch.0.clone().into_os_string());
    assert_eq!(find_program("gh", path.clone(), Some(".PS1;.CMD".into())), None);
    std::fs::write(scratch.0.join("gh.cmd"), b"x").unwrap();
    assert_eq!(
        find_program("gh", path, Some(".PS1;.CMD".into())),
        Some(scratch.0.join("gh.cmd"))
    );
}

#[test]
fn gh_output_of_the_wrong_shape_is_refused_with_what_it_printed() {
    let refused = |value: Value| records(value, "gh pr list", &PR_TYPES).unwrap_err();
    assert!(refused(json!({})).starts_with("gh pr list printed a JSON object, not a list of objects"));
    assert!(refused(json!([1])).starts_with("gh pr list printed a list holding a number, not objects"));
    assert!(refused(json!([{"title": "t"}])).starts_with("gh pr list printed an item with no number"));
    assert!(refused(json!([{"number": "7"}])).starts_with("gh pr list printed number as a string"));
    assert!(
        refused(json!([{"number": 7, "statusCheckRollup": [{"name": 3}]}]))
            .starts_with("gh pr list printed name as a number")
    );
    assert!(records(json!([{"number": 7, "title": null}]), "gh pr list", &PR_TYPES).is_ok());
}

/// gh answering a pull request list, then the runs on its head.
struct Scripted {
    prs: Value,
    runs: Value,
    calls: Vec<String>,
}
impl Gh for Scripted {
    async fn call(&mut self, argv: &[&str]) -> Result<String, GhError> {
        self.calls.push(argv.join(" "));
        Ok(match argv {
            ["pr", "list", ..] => self.prs.to_string(),
            ["run", "list", ..] => self.runs.to_string(),
            _ => "null".into(),
        })
    }
}

fn block<T>(future: impl std::future::Future<Output = T>) -> T {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(future)
}

#[test]
fn a_green_head_waits_for_its_queued_runs_and_a_quiet_poll_costs_one_call() {
    let prs = json!([{
        "number": 5, "title": "t", "headRefOid": "c".repeat(40), "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"__typename": "CheckRun", "name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }]);
    let runs = json!([{"databaseId": 9, "workflowDatabaseId": 1, "workflowName": "Rust", "status": "queued"}]);
    let mut gh = Scripted { prs, runs, calls: Vec::new() };
    let snap = block(GitHub { gh: &mut gh, author: Some("<user>".into()) }.prs(None)).unwrap();
    assert_eq!(snap["5"]["checks"], "pending");
    assert!(gh.calls[0].ends_with("--author <user>"), "{:?}", gh.calls);
    assert!(gh.calls[1].starts_with(&format!("run list --commit {}", "c".repeat(40))));
    // The same head already known green: no runs lookup.
    gh.runs = json!([]);
    let mut known = snap.clone();
    known["5"]["checks"] = json!("green");
    gh.calls.clear();
    block(GitHub { gh: &mut gh, author: None }.prs(Some(&known))).unwrap();
    assert_eq!(gh.calls.len(), 1, "{:?}", gh.calls);
}

#[test]
fn suite_results_are_read_once_written_and_a_non_object_is_noted_once() {
    let scratch = Scratch::new();
    std::fs::write(
        scratch.0.join("a.json"),
        "\u{feff}{\"machine\": \"box\", \"commit\": \"0123456789abcdef\", \"exit_code\": 0, \"summary\": \"ok\"}",
    )
    .unwrap();
    std::fs::write(scratch.0.join("b.json"), "{\"machine\": ").unwrap();
    std::fs::write(scratch.0.join("c.json"), "[1]").unwrap();
    std::fs::write(scratch.0.join("d.txt"), "{}").unwrap();
    let mut skipped = Vec::new();
    let mut notes = Vec::new();
    let snap = suites(&scratch.0, None, &mut skipped, &mut |note| notes.push(note));
    assert_eq!(
        Value::Object(snap.clone()),
        json!({"a.json": {"machine": "box", "commit": "01234567", "exit": 0, "summary": "ok"}})
    );
    assert_eq!(notes.len(), 1);
    assert!(notes[0].ends_with("is a list, not an object; skipped until it holds a result"), "{notes:?}");
    // Written in full later: reported then; the non-object is not noted again.
    std::fs::write(scratch.0.join("b.json"), "{\"machine\": \"wsl\"}").unwrap();
    let again = suites(&scratch.0, Some(&snap), &mut skipped, &mut |note| notes.push(note));
    assert_eq!(again["b.json"]["machine"], "wsl");
    assert_eq!(again["b.json"]["exit"], Value::Null);
    assert_eq!(notes.len(), 1);
}
