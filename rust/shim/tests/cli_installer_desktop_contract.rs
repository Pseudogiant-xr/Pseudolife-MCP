//! `pseudolife-mcp installer claude-desktop`: the real binary in a
//! throwaway, private home, registering itself (so the token-file probe
//! reads the native client's own `--help`). Never the real Desktop config.
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
};

const TOKEN: &str = "t0k3n-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx";

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = pseudolife_stdio::setup::codex::resolved_path(&std::env::temp_dir())
            .unwrap()
            .join(format!("w7-desktop-contract-{}", uuid::Uuid::new_v4()));
        pseudolife_stdio::board::state::private_dir(&path).unwrap();
        Self(path)
    }
    fn config(&self) -> PathBuf {
        if cfg!(windows) {
            self.0.join("Roaming/Claude/claude_desktop_config.json")
        } else if cfg!(target_os = "macos") {
            self.0
                .join("Library/Application Support/Claude/claude_desktop_config.json")
        } else {
            self.0.join(".config/Claude/claude_desktop_config.json")
        }
    }
    fn run(&self, args: &[&str]) -> Output {
        let binary = env!("CARGO_BIN_EXE_pseudolife-stdio");
        let mut command = Command::new(binary);
        command.env_clear();
        for key in ["SystemRoot", "WINDIR", "TEMP", "TMP"] {
            if let Some(value) = std::env::var_os(key) {
                command.env(key, value);
            }
        }
        command
            .env("HOME", &self.0)
            .env("USERPROFILE", &self.0)
            .env("LOCALAPPDATA", self.0.join("Local"))
            .env("APPDATA", self.0.join("Roaming"))
            .env("PATH", self.0.join("empty-path"))
            .env("PSEUDOLIFE_DESKTOP_TOKEN_SOURCE", TOKEN)
            .env("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9")
            .args(["installer", "claude-desktop", "--command", binary])
            .args(args)
            .current_dir(&self.0)
            .output()
            .unwrap()
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn all_text(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

#[test]
fn registers_itself_with_a_private_token_file_then_reruns_quietly() {
    let home = Home::new();
    let token_file = home.0.join("pseudolife/claude-desktop.token");
    let token_text = token_file.to_string_lossy().into_owned();
    let args = [
        "--writer-id",
        "claude-desktop",
        "--default-token-file",
        &token_text,
        "--token-from-env",
        "PSEUDOLIFE_DESKTOP_TOKEN_SOURCE",
    ];
    let first = home.run(&args);
    let text = all_text(&first);
    assert_eq!(first.status.code(), Some(0), "{text}");
    assert!(
        text.contains("reads PSEUDOLIFE_MCP_TOKEN_FILE"),
        "the probe read the real help: {text}"
    );
    assert!(text.contains("entry: written"), "{text}");
    assert!(!text.contains(TOKEN));
    assert_eq!(fs::read(&token_file).unwrap(), TOKEN.as_bytes());
    pseudolife_stdio::board::state::open(&token_file).expect("token file is owner-only");
    let config: Value = serde_json::from_slice(&fs::read(home.config()).unwrap()).unwrap();
    assert_eq!(
        config["mcpServers"]["pseudolife-desktop"],
        json!({"command": env!("CARGO_BIN_EXE_pseudolife-stdio"), "env": {
            "PSEUDOLIFE_WRITER_ID": "claude-desktop", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
            "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
            "PSEUDOLIFE_MCP_TOKEN_FILE": token_text}})
    );
    let bytes = fs::read(home.config()).unwrap();
    let again = home.run(&args);
    let text = all_text(&again);
    assert_eq!(again.status.code(), Some(0), "{text}");
    assert!(text.contains("entry: unchanged"), "{text}");
    assert_eq!(fs::read(home.config()).unwrap(), bytes);
}

#[test]
fn json_report_goes_to_stdout_and_human_lines_to_stderr() {
    let home = Home::new();
    let output = home.run(&["--json"]);
    assert_eq!(output.status.code(), Some(0), "{}", all_text(&output));
    let report: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["state"], "written");
    assert_eq!(report["exit"], 0);
    assert!(String::from_utf8_lossy(&output.stderr).contains("entry: written"));
}

#[test]
fn a_malformed_config_is_refused_with_exit_2_and_left_alone() {
    let home = Home::new();
    fs::create_dir_all(home.config().parent().unwrap()).unwrap();
    fs::write(home.config(), b"{not json").unwrap();
    let output = home.run(&[]);
    assert_eq!(output.status.code(), Some(2), "{}", all_text(&output));
    assert!(String::from_utf8_lossy(&output.stderr).contains("not valid JSON"));
    assert_eq!(fs::read(home.config()).unwrap(), b"{not json");
}

#[test]
fn no_credential_for_a_token_file_exits_3_with_the_entry_written() {
    let home = Home::new();
    let token_file = home.0.join("missing.token");
    let output = home.run(&["--default-token-file", &token_file.to_string_lossy()]);
    let text = all_text(&output);
    assert_eq!(output.status.code(), Some(3), "{text}");
    assert!(text.contains("WITHOUT a credential"), "{text}");
    assert!(home.config().is_file());
}

/// The Store (MSIX) Desktop's config folder grants a package capability SID
/// full control by inheritance, so the file Desktop created there carries
/// it. The shared writer accepts that file since #1549.
#[cfg(windows)]
#[test]
fn the_store_desktop_config_with_an_inherited_capability_ace_is_registered() {
    let home = Home::new();
    let cache = home
        .0
        .join("Local/Packages/Claude_w7fixture/LocalCache/Roaming/Claude");
    fs::create_dir_all(&cache).unwrap();
    let sddl = "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-15-3-1024-1065365936-1281604716-3511738428-1654721687-432734479-3232135806-4053264122-3456934681)";
    // Built with the Windows API, not a PowerShell child: a Windows
    // PowerShell started from a PowerShell 7 parent inherits its module path
    // and cannot load Get-Acl (hosted windows-latest, stress 43536389201).
    acl::set_dacl(&cache, sddl).expect("fixture could not build the Store folder's ACL");
    let config = cache.join("claude_desktop_config.json");
    fs::write(&config, br#"{"mcpServers": {"other": {"command": "/x"}}}"#).unwrap();
    let inherited = || acl::dacl(&config).expect("fixture could not read the config's ACL");
    assert!(
        inherited().contains("S-1-15-3-1024"),
        "fixture: the file did not inherit the capability ACE: {}",
        inherited()
    );
    let output = home.run(&[]);
    let text = all_text(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(text.contains("entry: written"), "{text}");
    let written: Value = serde_json::from_slice(&fs::read(&config).unwrap()).unwrap();
    assert_eq!(written["mcpServers"]["other"]["command"], "/x");
    assert!(written["mcpServers"]["pseudolife-desktop"].is_object());
    assert!(
        inherited().contains("S-1-15-3-1024"),
        "the file keeps its ACL"
    );
    // The %APPDATA% copy was never created: the package cache wins.
    assert!(!home.config().exists());
}

/// Under umask 002 (a private group per user) a config the user created is
/// group-writable; the shared writer accepts it since #1549.
#[cfg(unix)]
#[test]
fn a_group_writable_desktop_config_is_registered() {
    use std::os::unix::fs::PermissionsExt;
    let home = Home::new();
    fs::create_dir_all(home.config().parent().unwrap()).unwrap();
    fs::write(home.config(), br#"{"mcpServers": {}}"#).unwrap();
    fs::set_permissions(home.config(), fs::Permissions::from_mode(0o664)).unwrap();
    let output = home.run(&[]);
    assert_eq!(output.status.code(), Some(0), "{}", all_text(&output));
    let mode = fs::metadata(home.config()).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o664, "the file keeps its mode");
}

/// DACL fixtures through the Windows API (SDDL in, SDDL out).
#[cfg(windows)]
mod acl {
    use std::{io, os::windows::ffi::OsStrExt, path::Path, ptr};
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::{
            Authorization::{
                ConvertSecurityDescriptorToStringSecurityDescriptorW,
                ConvertStringSecurityDescriptorToSecurityDescriptorW, GetNamedSecurityInfoW,
                SE_FILE_OBJECT,
            },
            DACL_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION, SetFileSecurityW,
        },
    };

    fn wide(text: &std::ffi::OsStr) -> Vec<u16> {
        text.encode_wide().chain([0]).collect()
    }

    /// Replace `path`'s DACL with `sddl` (protected, as written).
    pub fn set_dacl(path: &Path, sddl: &str) -> io::Result<()> {
        let text: Vec<u16> = sddl.encode_utf16().chain([0]).collect();
        let name = wide(path.as_os_str());
        // SAFETY: NUL-terminated buffers live through both calls; the
        // descriptor is freed after SetFileSecurityW copied it.
        unsafe {
            let mut descriptor = ptr::null_mut();
            if ConvertStringSecurityDescriptorToSecurityDescriptorW(
                text.as_ptr(),
                1,
                &mut descriptor,
                ptr::null_mut(),
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            let set = SetFileSecurityW(
                name.as_ptr(),
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                descriptor,
            );
            let error = io::Error::last_os_error();
            LocalFree(descriptor);
            if set == 0 {
                return Err(error);
            }
        }
        Ok(())
    }

    /// `path`'s DACL as SDDL.
    pub fn dacl(path: &Path) -> io::Result<String> {
        let name = wide(path.as_os_str());
        // SAFETY: the descriptor and the string are LocalAlloc'd by the API
        // and freed here; the string is read up to its returned length.
        unsafe {
            let mut descriptor = ptr::null_mut();
            let status = GetNamedSecurityInfoW(
                name.as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                &mut descriptor,
            );
            if status != 0 {
                return Err(io::Error::from_raw_os_error(status as i32));
            }
            let mut text = ptr::null_mut();
            let mut length = 0u32;
            let converted = ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,
                1,
                DACL_SECURITY_INFORMATION,
                &mut text,
                &mut length,
            );
            let error = io::Error::last_os_error();
            LocalFree(descriptor);
            if converted == 0 {
                return Err(error);
            }
            let sddl = String::from_utf16_lossy(std::slice::from_raw_parts(text, length as usize))
                .trim_end_matches('\0')
                .to_owned();
            LocalFree(text.cast());
            Ok(sddl)
        }
    }
}
