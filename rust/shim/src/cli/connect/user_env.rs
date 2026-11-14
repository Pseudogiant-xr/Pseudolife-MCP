//! The Windows User-scope environment (`HKCU\Environment`), and the
//! Machine-scope value it overrides, for the daemon URL. `connect` re-points
//! a User-scope `PSEUDOLIFE_MCP_DAEMON_URL` that exists and names another
//! daemon, under the run's one confirmation; it never creates one (the
//! registrations, the Claude Code settings copy and the Codex connection
//! file carry the URL for every client), and it never writes Machine scope.
//! Elsewhere every read is `None`.
use std::{cell::RefCell, rc::Rc};

/// A registry string value: its type (`REG_SZ` or `REG_EXPAND_SZ`) and data.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Value {
    pub kind: String,
    pub data: String,
}

pub(super) trait Backend {
    fn user(&self, name: &str) -> Result<Option<Value>, String>;
    fn machine(&self, name: &str) -> Result<Option<Value>, String>;
    fn set_user(&self, name: &str, value: &Value) -> Result<(), String>;
    /// Tell running programs the environment changed (`WM_SETTINGCHANGE`).
    fn broadcast(&self);
}

thread_local! {
    static OVERRIDE: RefCell<Option<Rc<dyn Backend>>> = const { RefCell::new(None) };
}

/// The backend in use: the registry, or a test's fake.
pub(super) fn backend() -> Rc<dyn Backend> {
    OVERRIDE
        .with(|slot| slot.borrow().clone())
        .unwrap_or_else(|| Rc::new(Registry))
}

#[cfg(test)]
pub(super) fn with_backend<T>(backend: Rc<dyn Backend>, run: impl FnOnce() -> T) -> T {
    OVERRIDE.with(|slot| *slot.borrow_mut() = Some(backend));
    let result = run();
    OVERRIDE.with(|slot| *slot.borrow_mut() = None);
    result
}

/// The PowerShell line that sets `name` in `scope` to `value`, as a fix.
pub(super) fn powershell_set(name: &str, value: &str, scope: &str) -> String {
    format!("[Environment]::SetEnvironmentVariable(\"{name}\", \"{value}\", \"{scope}\")")
}

const USER_KEY: &str = r"HKCU\Environment";
const MACHINE_KEY: &str = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment";

struct Registry;

impl Registry {
    fn reg() -> Result<std::path::PathBuf, String> {
        let root = std::env::var_os("SystemRoot")
            .filter(|value| !value.is_empty())
            .ok_or("SystemRoot is not set")?;
        Ok(std::path::PathBuf::from(root)
            .join("System32")
            .join("reg.exe"))
    }

    fn query(key: &str, name: &str) -> Result<Option<Value>, String> {
        if !cfg!(windows) {
            return Ok(None);
        }
        let output = std::process::Command::new(Self::reg()?)
            .args(["query", key, "/v", name])
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .output()
            .map_err(|error| format!("reg.exe could not be started: {error}"))?;
        if !output.status.success() {
            // reg.exe exits 1 for a missing value as for a missing key.
            return Ok(None);
        }
        Ok(parse_query(&String::from_utf8_lossy(&output.stdout), name))
    }
}

/// `reg query` prints `    <name>    <type>    <data>` for the value.
fn parse_query(text: &str, name: &str) -> Option<Value> {
    text.lines().find_map(|line| {
        let mut parts = line.trim_start().splitn(3, "    ");
        let found = parts.next()?;
        if !found.eq_ignore_ascii_case(name) {
            return None;
        }
        let kind = parts.next()?.trim().to_owned();
        let data = parts
            .next()
            .unwrap_or("")
            .trim_end_matches(['\r', '\n'])
            .to_owned();
        kind.starts_with("REG_").then_some(Value { kind, data })
    })
}

impl Backend for Registry {
    fn user(&self, name: &str) -> Result<Option<Value>, String> {
        Self::query(USER_KEY, name)
    }

    fn machine(&self, name: &str) -> Result<Option<Value>, String> {
        Self::query(MACHINE_KEY, name)
    }

    fn set_user(&self, name: &str, value: &Value) -> Result<(), String> {
        if !cfg!(windows) {
            return Err("there is no Windows User-scope environment on this platform".into());
        }
        let kind = if value.kind == "REG_EXPAND_SZ" {
            "REG_EXPAND_SZ"
        } else {
            "REG_SZ"
        };
        let output = std::process::Command::new(Self::reg()?)
            .args([
                "add",
                USER_KEY,
                "/v",
                name,
                "/t",
                kind,
                "/d",
                &value.data,
                "/f",
            ])
            .stdin(std::process::Stdio::null())
            .output()
            .map_err(|error| format!("reg.exe could not be started: {error}"))?;
        if !output.status.success() {
            let text = String::from_utf8_lossy(&output.stderr);
            let line = text.trim().lines().last().unwrap_or("").trim();
            return Err(if line.is_empty() {
                format!("reg.exe exited with {}", output.status.code().unwrap_or(1))
            } else {
                line.to_owned()
            });
        }
        // Read it back: the registry, not reg.exe's exit, says what is set.
        match Self::query(USER_KEY, name)? {
            Some(found) if found.data == value.data => Ok(()),
            _ => Err("the new value did not read back".into()),
        }
    }

    #[allow(unsafe_code)]
    fn broadcast(&self) {
        #[cfg(windows)]
        {
            use windows_sys::Win32::UI::WindowsAndMessaging::{
                HWND_BROADCAST, SMTO_ABORTIFHUNG, SendMessageTimeoutW, WM_SETTINGCHANGE,
            };
            let area: Vec<u16> = "Environment".encode_utf16().chain([0]).collect();
            let mut result = 0;
            // SAFETY: the NUL-terminated "Environment" buffer outlives the
            // call, and the result pointer is a live local; a hung window
            // makes the call return after the timeout, never block.
            unsafe {
                SendMessageTimeoutW(
                    HWND_BROADCAST,
                    WM_SETTINGCHANGE,
                    0,
                    area.as_ptr() as isize,
                    SMTO_ABORTIFHUNG,
                    // Per window: a slow one cannot stall the run for long.
                    1000,
                    &mut result,
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reg_query_output_is_read_with_its_type() {
        let text = "\r\nHKEY_CURRENT_USER\\Environment\r\n    PSEUDOLIFE_MCP_DAEMON_URL    REG_EXPAND_SZ    http://10.0.0.5:8765\r\n\r\n";
        assert_eq!(
            parse_query(text, "PSEUDOLIFE_MCP_DAEMON_URL"),
            Some(Value {
                kind: "REG_EXPAND_SZ".into(),
                data: "http://10.0.0.5:8765".into()
            })
        );
        // An empty value still has its type; another name is not this one.
        let empty = "    PSEUDOLIFE_MCP_DAEMON_URL    REG_SZ    \r\n";
        assert_eq!(
            parse_query(empty, "PSEUDOLIFE_MCP_DAEMON_URL")
                .unwrap()
                .data,
            ""
        );
        assert_eq!(
            parse_query("    OTHER    REG_SZ    x\r\n", "PSEUDOLIFE_MCP_DAEMON_URL"),
            None
        );
    }
}

/// The real registry, under a variable name unique to the run, removed
/// afterwards; run on the Windows dev VM only (`--ignored`).
#[cfg(all(test, windows))]
mod registry_tests {
    use super::*;

    #[test]
    #[ignore = "writes the real User-scope environment under a unique name; run on the Windows dev VM only"]
    fn the_registry_backend_writes_reads_back_and_broadcasts_a_user_value() {
        let name = format!(
            "PSEUDOLIFE_CONNECT_TEST_{}",
            uuid::Uuid::new_v4().simple().to_string().to_uppercase()
        );
        let wanted = Value {
            kind: "REG_EXPAND_SZ".into(),
            data: "http://10.0.0.9:8765".into(),
        };
        assert_eq!(Registry.user(&name), Ok(None));
        Registry.set_user(&name, &wanted).unwrap();
        assert_eq!(Registry.user(&name), Ok(Some(wanted.clone())));
        Registry.broadcast();
        let removed = std::process::Command::new(Registry::reg().unwrap())
            .args(["delete", USER_KEY, "/v", &name, "/f"])
            .output()
            .unwrap();
        assert!(removed.status.success());
        assert_eq!(Registry.user(&name), Ok(None), "the test value is gone");
    }
}
