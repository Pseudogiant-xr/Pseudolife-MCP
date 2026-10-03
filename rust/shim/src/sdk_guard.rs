//! Preserve the selected Python runtime's pre-daemon capability diagnostic.
use crate::lifecycle::StartupError;
use process_wrap::tokio::*;
use std::{path::Path, process::Stdio};
pub const PROBE_MODULE: &str = "mcp.server.subscriptions";
fn fatal(message: impl Into<String>) -> StartupError {
    StartupError {
        code: 1,
        message: message.into(),
    }
}
struct OwnedProbe(Box<dyn ChildWrapper>);
impl Drop for OwnedProbe {
    fn drop(&mut self) {
        let _ = self.0.start_kill();
    }
}
const PROBE: &str = r#"import importlib.util, json, sys
try:
    present = importlib.util.find_spec(sys.argv[1]) is not None
except ImportError:
    present = False
if present:
    print(json.dumps({'present': True}))
else:
    try:
        from importlib.metadata import version
        installed = version('mcp')
    except Exception:
        installed = 'not installed'
    print(json.dumps({'present': False, 'installed': installed, 'executable': sys.executable}))
"#;

pub async fn require_sdk(python: &Path, module: &str) -> Result<(), StartupError> {
    let mut command = CommandWrap::with_new(python, |command| {
        command
            .args(["-c", PROBE, module])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
    });
    command.wrap(KillOnDrop);
    #[cfg(unix)]
    command.wrap(ProcessSession);
    #[cfg(windows)]
    {
        let mut flags = CreationFlags(Default::default());
        flags.0.0 = crate::lifecycle::WINDOWS_DETACHED_FLAGS;
        command.wrap(flags).wrap(JobObject);
    }
    let mut child =
        OwnedProbe(command.spawn().map_err(|_| {
            fatal("[shim] could not inspect the selected Python runtime's MCP SDK")
        })?);
    use tokio::io::AsyncReadExt;
    let mut stdout = child.0.stdout().take().expect("piped SDK probe stdout");
    let mut bytes = Vec::new();
    let read = stdout.read_to_end(&mut bytes);
    tokio::pin!(read);
    let mut completed_read = None;
    // This wrapper's direct inner layer is the native leader. Waiting for the
    // entire group first would wait on a package import's surviving children.
    let status = tokio::select! {
        result = &mut read => { completed_read = Some(result); child.0.inner_mut().wait().await },
        status = child.0.inner_mut().wait() => status,
    };
    // Reap the complete process group/job, including a package import's children.
    // The probe itself never needs a surviving background worker.
    let _ = Box::into_pin(child.0.kill()).await;
    let read = match completed_read {
        Some(result) => result,
        None => read.await,
    };
    let value: serde_json::Value = read
        .ok()
        .and_then(|_| serde_json::from_slice(&bytes).ok())
        .filter(|_| status.as_ref().is_ok_and(|status| status.success()))
        .ok_or_else(|| fatal("[shim] could not inspect the selected Python runtime's MCP SDK"))?;
    if value["present"] == true {
        return Ok(());
    }
    let installed = value["installed"].as_str().unwrap_or("not installed");
    let executable = value["executable"]
        .as_str()
        .unwrap_or_else(|| python.to_str().unwrap_or("python"));
    Err(fatal(format!(
        "[shim] this environment's MCP SDK (mcp {installed}) predates v2 — the shim needs mcp>=2.1 (no {module}).\n  Fix:  \"{executable}\" -m pip install -U \"mcp>=2.1,<3\"\n  (or re-run the repo installer, which registers the project venv's shim)"
    )))
}
