use super::Output;
use super::autostart::{Registered, check_autostart, classify, move_command};
use super::registration_text;
use std::{
    fs,
    path::{Path, PathBuf},
};

/// `ops/install-shim-autostart.sh` / `ops/install-codex-shim-autostart.sh`,
/// as `systemctl --user cat` prints the unit.
fn python_unit(kind: &str, repo: &str) -> String {
    format!(
        "# /home/<user>/.config/systemd/user/pseudolife-sonnet-shim.service\n[Unit]\nDescription=Claude extractor CLI shim (dream pass primary; E4B sidecar is fallback)\nAfter=network-online.target\n\n[Service]\nExecStart=\"/home/<user>/.venv/bin/python\" \"{repo}/ops/shim_autostart.py\" run {kind} --foreground\nWorkingDirectory={repo}\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n"
    )
}

/// The native unit `autostart.sh` writes.
fn native_unit(kind: &str, description: &str) -> String {
    format!(
        "[Unit]\nDescription={description}\n\n[Service]\nExecStart=\"/home/<user>/.local/bin/pseudolife-mcp\" \"extractor-shim\" \"{kind}\" \"--env-file\" \"/home/<user>/repo/ops/.env\" \"--repo\" \"/home/<user>/repo\"\nRestart=on-failure\n"
    )
}

fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(TABLE[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// A `schtasks /Query /XML` task whose action is PowerShell running `code`
/// as an encoded command, the way every Python-era and native installer
/// registers it.
fn encoded_task(code: &str, description: &str) -> Vec<u8> {
    let utf16: Vec<u8> = code.encode_utf16().flat_map(u16::to_le_bytes).collect();
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\n<Task version=\"1.2\">\n  <RegistrationInfo>\n    <Description>{description}</Description>\n  </RegistrationInfo>\n  <Settings>\n    <Enabled>true</Enabled>\n  </Settings>\n  <Actions Context=\"Author\">\n    <Exec>\n      <Command>powershell.exe</Command>\n      <Arguments>-NoProfile -WindowStyle Hidden -EncodedCommand {}</Arguments>\n    </Exec>\n  </Actions>\n</Task>\n",
        base64(&utf16)
    )
    .into_bytes()
}

/// `$inner` of `ops/install-shim-autostart.ps1` (and the Codex one), with
/// its `-replace "'", "''"` applied.
fn python_task_code(kind: &str, python: &str, repo: &str) -> String {
    let q = |s: &str| s.replace('\'', "''");
    format!(
        "$psi = New-Object System.Diagnostics.ProcessStartInfo\r\n$psi.FileName = '{}'\r\n$psi.Arguments = '{}'\r\n$psi.UseShellExecute = $false\r\n$psi.CreateNoWindow = $true\r\n$psi.WorkingDirectory = '{}'\r\n[System.Diagnostics.Process]::Start($psi) | Out-Null",
        q(python),
        q(&format!("\"{repo}\\ops\\shim_autostart.py\" run {kind}")),
        q(repo)
    )
}

/// `Get-NativeTaskCode` in `extractor_shim/autostart.ps1`.
fn native_task_code(kind: &str) -> String {
    format!(
        "$psi = New-Object System.Diagnostics.ProcessStartInfo\n$psi.FileName = 'C:\\Users\\<user>\\AppData\\Local\\pseudolife-mcp\\bin\\pseudolife-mcp.exe'\n$psi.Arguments = 'extractor-shim {kind} --env-file \"C:\\Users\\<user>\\repo\\ops\\.env\"'\n$psi.UseShellExecute = $false\n$psi.CreateNoWindow = $true\n$process = [System.Diagnostics.Process]::Start($psi)\n$process.WaitForExit()\nexit $process.ExitCode"
    )
}

#[test]
fn the_python_runner_unit_names_its_checkout() {
    for kind in ["claude", "codex"] {
        assert_eq!(
            classify(&python_unit(kind, "/home/<user>/Pseudolife MCP"), kind),
            Registered::PythonRunner {
                repo: PathBuf::from("/home/<user>/Pseudolife MCP")
            }
        );
    }
}

#[test]
fn the_python_runner_task_names_its_checkout() {
    for kind in ["claude", "codex"] {
        for python in [
            r"C:\Users\<user>\repo\.venv\Scripts\python.exe",
            r"C:\Program Files\Python311\pythonw.exe",
        ] {
            let code = python_task_code(kind, python, r"C:\Users\<user>\it's repo");
            let text = registration_text(&encoded_task(&code, "Claude extractor CLI shim"));
            assert_eq!(
                classify(&text, kind),
                Registered::PythonRunner {
                    repo: PathBuf::from(r"C:\Users\<user>\it's repo")
                },
                "{python}"
            );
        }
    }
}

#[test]
fn a_runner_started_by_something_other_than_python_is_not_moved() {
    let text = registration_text(&encoded_task(
        &python_task_code("claude", r"C:\tools\launcher.exe", r"C:\repo"),
        "",
    ));
    assert_eq!(classify(&text, "claude"), Registered::PythonOther);
    let unit =
        python_unit("claude", "/srv/repo").replace("/home/<user>/.venv/bin/python", "/usr/bin/env");
    assert_eq!(classify(&unit, "claude"), Registered::PythonOther);
}

#[test]
fn the_runner_for_the_other_provider_is_not_this_ones() {
    assert_eq!(
        classify(&python_unit("codex", "/srv/repo"), "claude"),
        Registered::PythonOther
    );
    let text = registration_text(&encoded_task(
        &python_task_code("codex", r"C:\Python311\python.exe", r"C:\repo"),
        "",
    ));
    assert_eq!(classify(&text, "claude"), Registered::PythonOther);
}

#[test]
fn a_native_registration_is_left_alone_even_when_its_description_names_the_runner() {
    for kind in ["claude", "codex"] {
        assert_eq!(
            classify(
                &native_unit(kind, "replaces ops/shim_autostart.py run --model"),
                kind
            ),
            Registered::Other
        );
        let text = registration_text(&encoded_task(
            &native_task_code(kind),
            "replaces ops/shim_autostart.py run --model",
        ));
        assert_eq!(classify(&text, kind), Registered::Other);
    }
}

#[test]
fn other_python_forms_are_reported_not_moved() {
    // A relative checkout, a runner with a different tail, the shim script
    // itself with baked values: no exact runner, so only a report.
    for unit in [
        python_unit("claude", "repo"),
        python_unit("claude", "/srv/repo").replace(" --foreground", ""),
        "[Service]\nExecStart=/usr/bin/python3 /srv/repo/evals/claude_shim.py --port 8082 --model claude-opus-5\n".into(),
    ] {
        assert_eq!(classify(&unit, "claude"), Registered::PythonOther, "{unit}");
    }
    let mut two = python_unit("claude", "/srv/repo");
    two.push_str("[Service]\nExecStart=\nExecStart=/usr/bin/python3 /srv/other/ops/shim_autostart.py run claude --foreground\n");
    assert_eq!(classify(&two, "claude"), Registered::PythonOther);
}

#[test]
fn a_disabled_task_is_reported() {
    let text = String::from_utf8(encoded_task(&native_task_code("claude"), ""))
        .unwrap()
        .replace("<Enabled>true</Enabled>", "<Enabled>false</Enabled>");
    assert_eq!(classify(&text, "claude"), Registered::Disabled);
}

struct Checkout(PathBuf);
impl Checkout {
    fn new(log: Option<&str>) -> Self {
        let root = std::env::temp_dir().join(format!("autostart-report-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(root.join("ops")).unwrap();
        let mut env = "PSEUDOLIFE_CLAUDE_SHIM_PORT=8082\n".to_owned();
        if let Some(log) = log {
            env.push_str(&format!("PSEUDOLIFE_CLAUDE_SHIM_LOG='{log}'\n"));
        }
        fs::write(root.join("ops/.env"), env).unwrap();
        Self(root)
    }
}
impl Drop for Checkout {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// The step over a Python runner registration of `repo` (this platform's
/// form); `native` is the stable launcher when it is the native client's.
fn report(repo: &Path, native: Option<&Path>) -> serde_json::Value {
    let text = if cfg!(windows) {
        encoded_task(
            &python_task_code(
                "claude",
                r"C:\Python311\python.exe",
                &repo.display().to_string(),
            ),
            "",
        )
    } else {
        python_unit("claude", &repo.display().to_string()).into_bytes()
    };
    let query = |kind: &str, _task: &str, _unit: &str| {
        (kind == "claude").then(|| Output {
            code: 0,
            bytes: text.clone(),
        })
    };
    check_autostart(&query, native, Path::new("/fixture/bin/pseudolife-mcp"))
}

#[test]
fn a_python_runner_is_reported_with_the_command_that_moves_it() {
    let repo = Checkout::new(Some("/var/log/my shim.log"));
    let launcher = Path::new("/fixture/bin/pseudolife-mcp");
    let report = report(&repo.0, Some(launcher));
    assert_eq!(report["state"], "stale", "{report}");
    let detail = report["detail"].as_str().unwrap();
    assert!(detail.contains("legacy Python runner"), "{detail}");
    assert!(
        detail.contains("to move it to the native shim with the same settings, run "),
        "{detail}"
    );
    assert!(
        detail.ends_with(&move_command(launcher, "claude", &repo.0)),
        "{detail}"
    );
    let command = move_command(launcher, "claude", &repo.0);
    for word in [
        "extractor-shim",
        "install-autostart",
        "claude",
        "--repo",
        "--native-exe",
        "--log",
    ] {
        assert!(command.contains(word), "{word}: {command}");
    }
    assert!(command.contains("my shim.log"), "{command}");
}

#[test]
fn without_the_native_client_the_report_says_to_move_the_machine_first() {
    let repo = Checkout::new(None);
    let report = report(&repo.0, None);
    assert_eq!(report["state"], "stale", "{report}");
    let detail = report["detail"].as_str().unwrap();
    assert!(
        detail.contains(
            "once this machine runs the native client (pseudolife-mcp update --clients-only)"
        ),
        "{detail}"
    );
}

#[test]
fn a_relative_log_is_the_checkouts() {
    let repo = Checkout::new(Some("logs/claude.log"));
    let command = move_command(Path::new("/fixture/bin/pseudolife-mcp"), "claude", &repo.0);
    let expected = repo.0.join("logs/claude.log").display().to_string();
    assert!(command.contains(&expected), "{expected}: {command}");
    let home = Checkout::new(Some("~/.pseudolife-mcp/claude-shim.log"));
    let command = move_command(Path::new("/fixture/bin/pseudolife-mcp"), "claude", &home.0);
    assert!(
        command.contains("~/.pseudolife-mcp/claude-shim.log"),
        "{command}"
    );
}

#[test]
fn a_powershell_command_quotes_every_apostrophe_it_reads() {
    if !cfg!(windows) {
        return;
    }
    let repo = Checkout::new(None);
    let command = move_command(
        Path::new(r"C:\Team’s tools\pseudolife-mcp.exe"),
        "claude",
        &repo.0,
    );
    assert!(
        command.starts_with(r"& 'C:\Team’’s tools\pseudolife-mcp.exe' "),
        "{command}"
    );
}

#[test]
fn a_native_registration_is_current() {
    let text = if cfg!(windows) {
        encoded_task(&native_task_code("claude"), "")
    } else {
        native_unit("claude", "Native claude extractor CLI shim").into_bytes()
    };
    let query = |kind: &str, _: &str, _: &str| {
        (kind == "claude").then(|| Output {
            code: 0,
            bytes: text.clone(),
        })
    };
    let launcher = Path::new("/fixture/bin/pseudolife-mcp");
    let report = check_autostart(&query, Some(launcher), launcher);
    assert_eq!(report["state"], "current", "{report}");
    let none = check_autostart(&|_: &str, _: &str, _: &str| None, Some(launcher), launcher);
    assert_eq!(none["state"], "none", "{none}");
}
