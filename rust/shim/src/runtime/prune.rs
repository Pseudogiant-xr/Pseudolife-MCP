use super::*;
use std::collections::BTreeSet;

// Reuse the client connector's read-only process inspection rather than a
// shell command, PID probe, or Windows console control event.
#[path = "../cli/connect/procs.rs"]
#[allow(dead_code)]
mod inspection;
pub type Process = (u32, u32, PathBuf);
#[derive(Clone, Debug)]
pub struct Evidence {
    pub processes: Vec<Process>,
    pub registrations: BTreeSet<PathBuf>,
    pub tunnels: BTreeSet<PathBuf>,
}
pub fn default_tunnel_root() -> PathBuf {
    home().join(".pseudolife-mcp/tunnel")
}
pub fn collect_evidence(layout: &Layout) -> Result<Evidence> {
    let mut processes = inspection::list()
        .map_err(|_| "process table is unreadable; no runtime was removed")?
        .ok_or("this platform has no process table; no runtime was removed")?;
    verify_process_evidence(&mut processes)?;
    let registrations = registrations::pinned(layout, &registrations::find_registrations()?);
    let tunnels = tunnel_runtimes(layout, &default_tunnel_root(), &processes)?;
    Ok(Evidence {
        processes,
        registrations,
        tunnels,
    })
}

#[cfg(target_os = "linux")]
fn verify_process_evidence(rows: &mut Vec<Process>) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    for entry in fs::read_dir("/proc").map_err(io_error)? {
        let entry = entry.map_err(io_error)?;
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<u32>().ok())
        else {
            continue;
        };
        let path = entry.path();
        let metadata = match fs::metadata(&path) {
            Ok(m) => m,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(_) => return Err("process ownership is unreadable; no runtime was removed".into()),
        };
        if metadata.uid() != rustix::process::getuid().as_raw() {
            continue;
        }
        // argv[0] is user-controlled and can omit the executable path. Verify
        // the actual image as well, including native paths containing spaces.
        match fs::read_link(path.join("exe")) {
            Ok(image) => rows.push((pid, 0, image)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                let stat = fs::read_to_string(path.join("stat"));
                match stat {
                    Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                    Ok(stat)
                        if stat
                            .rsplit_once(')')
                            .is_some_and(|(_, s)| s.trim_start().starts_with('Z')) =>
                    {
                        continue;
                    }
                    _ => {
                        return Err(
                            "a live process image is unreadable; no runtime was removed".into()
                        );
                    }
                }
            }
            Err(_) => {
                return Err("a live process image is unreadable; no runtime was removed".into());
            }
        }
    }
    Ok(())
}
#[cfg(windows)]
#[allow(unsafe_code)]
fn verify_process_evidence(rows: &mut [Process]) -> Result<()> {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
        System::Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
            TH32CS_SNAPPROCESS,
        },
    };
    // The shared inspector skips processes it cannot query. For deletion that
    // absence is unknown evidence, even if the process probably belongs to
    // another account. Keep every runtime rather than infer its ownership.
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot.is_null() || snapshot == INVALID_HANDLE_VALUE {
            return Err("process snapshot is unreadable; no runtime was removed".into());
        }
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut more = Process32FirstW(snapshot, &mut entry);
        let mut unknown = more == 0;
        while more != 0 {
            if entry.th32ProcessID > 4 && !rows.iter().any(|r| r.0 == entry.th32ProcessID) {
                unknown = true;
            }
            more = Process32NextW(snapshot, &mut entry);
        }
        CloseHandle(snapshot);
        if unknown {
            return Err(
                "one or more process images could not be inspected; no runtime was removed".into(),
            );
        }
    }
    Ok(())
}
#[cfg(target_os = "macos")]
fn verify_process_evidence(rows: &mut Vec<Process>) -> Result<()> {
    let output = std::process::Command::new("/bin/ps")
        .args(["-axo", "pid=,ppid=,comm="])
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(io_error)?;
    if !output.status.success() {
        return Err("process image table is unreadable; no runtime was removed".into());
    }
    for line in std::str::from_utf8(&output.stdout)
        .map_err(|_| "process image table is not UTF-8")?
        .lines()
    {
        let mut parts = line.trim_start().splitn(2, char::is_whitespace);
        let pid = parts
            .next()
            .and_then(|s| s.parse::<u32>().ok())
            .ok_or("invalid process identity")?;
        let mut rest = parts
            .next()
            .ok_or("missing process image")?
            .trim_start()
            .splitn(2, char::is_whitespace);
        let ppid = rest
            .next()
            .and_then(|s| s.parse::<u32>().ok())
            .ok_or("invalid parent process identity")?;
        let image = rest
            .next()
            .ok_or("process image is unreadable; no runtime was removed")?
            .trim_start();
        if !image.starts_with('/') {
            return Err("process image is unknown; no runtime was removed".into());
        }
        rows.push((pid, ppid, PathBuf::from(image)));
    }
    Ok(())
}
#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn verify_process_evidence(_: &mut Vec<Process>) -> Result<()> {
    Err("process image inspection is unavailable; no runtime was removed".into())
}

pub fn tunnel_runtimes(
    layout: &Layout,
    root: &Path,
    processes: &[Process],
) -> Result<BTreeSet<PathBuf>> {
    let mut pinned = BTreeSet::new();
    crate::credentials::reject_ancestor_redirects(root)
        .map_err(|_| "tunnel directory is redirected or unreadable; no runtime was removed")?;
    match fs::symlink_metadata(root) {
        Ok(meta) if meta.is_dir() && !redirect(&meta) => {}
        Ok(_) => {
            return Err(
                "tunnel directory is redirected or not a directory; no runtime was removed".into(),
            );
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(pinned),
        Err(_) => return Err("tunnel directory is unreadable; no runtime was removed".into()),
    }
    for entry in
        fs::read_dir(root).map_err(|_| "tunnel directory is unreadable; no runtime was removed")?
    {
        let entry = entry.map_err(io_error)?;
        let name = entry.file_name();
        let name = name
            .to_str()
            .ok_or("tunnel record filename is not Unicode")?;
        if !name.ends_with(".process.json") && !name.ends_with(".reload.json") {
            continue;
        }
        let bytes = crate::board::state::read(&entry.path(), 1024 * 1024)
            .map_err(|_| "tunnel record is unreadable or not owner-only; no runtime was removed")?;
        let record: Value = serde_json::from_slice(&bytes)
            .map_err(|_| "invalid tunnel record; no runtime was removed")?;
        if !record.is_object() {
            return Err("tunnel record is not an object; no runtime was removed".into());
        }
        let ids: Vec<_> = [
            record.get("pid"),
            record.get("supervisor").and_then(|v| v.get("pid")),
            record.get("target").and_then(|v| v.get("pid")),
        ]
        .into_iter()
        .flatten()
        .filter_map(Value::as_u64)
        .collect();
        if ids.is_empty() && record.get("snapshot").is_some_and(|v| !v.is_null()) {
            return Err("tunnel snapshot has no process identity; no runtime was removed".into());
        }
        if !ids
            .iter()
            .any(|pid| processes.iter().any(|p| u64::from(p.0) == *pid))
        {
            continue;
        }
        // Native supervisors may pin a runtime directly. No profile credentials
        // or remote endpoints are read while determining local dependencies.
        for key in ["executable", "runtime_path", "interpreter"] {
            if let Some(path) = record.get(key).and_then(Value::as_str)
                && let Some(runtime) = registrations::runtime_path(Path::new(path), layout)
            {
                pinned.insert(runtime);
            }
        }
        let Some(snapshot) = record.get("snapshot").filter(|v| !v.is_null()) else {
            continue;
        };
        if !snapshot.is_object() {
            return Err("invalid tunnel snapshot; no runtime was removed".into());
        }
        for key in ["executable", "runtime_path", "interpreter"] {
            if let Some(path) = snapshot.get(key).and_then(Value::as_str)
                && let Some(runtime) = registrations::runtime_path(Path::new(path), layout)
            {
                pinned.insert(runtime);
            }
        }
        let digest = snapshot
            .get("digest")
            .and_then(Value::as_str)
            .filter(|d| valid_digest(d))
            .ok_or("tunnel snapshot has no valid digest; no runtime was removed")?;
        let bridge = root.join(".bridges").join(digest);
        if snapshot.get("kind").and_then(Value::as_str) == Some("native") {
            let bytes = crate::board::state::read(&bridge.join("runtime.json"), 16384)
                .map_err(|_| "native tunnel snapshot is unreadable; no runtime was removed")?;
            let native: Value =
                serde_json::from_slice(&bytes).map_err(|_| "invalid native tunnel snapshot")?;
            let executable = native
                .get("executable")
                .and_then(Value::as_str)
                .ok_or("native tunnel snapshot has no executable")?;
            if let Some(runtime) = registrations::runtime_path(Path::new(executable), layout) {
                pinned.insert(runtime);
            }
        } else {
            // Old Python snapshots remain evidence, never executable code. A
            // bounded literal parser replaces ast.literal_eval without Python.
            let bytes = crate::board::state::read(&bridge.join("launch.py"), 1024 * 1024)
                .map_err(|_| "legacy tunnel snapshot is unreadable; no runtime was removed")?;
            let text =
                std::str::from_utf8(&bytes).map_err(|_| "legacy tunnel snapshot is not UTF-8")?;
            let line = text
                .lines()
                .find_map(|l| l.strip_prefix("sys.path[1:1]="))
                .ok_or("legacy tunnel snapshot has no dependency list; no runtime was removed")?;
            for path in literal_paths(line)? {
                if let Some(runtime) = registrations::runtime_path(Path::new(&path), layout) {
                    pinned.insert(runtime);
                }
            }
        }
    }
    Ok(pinned)
}
fn literal_paths(text: &str) -> Result<Vec<String>> {
    let mut chars = text.trim().chars().peekable();
    let error = "invalid legacy tunnel dependency literal; no runtime was removed";
    if chars.next() != Some('[') {
        return Err(error.into());
    }
    let mut paths = Vec::new();
    loop {
        while chars.peek().is_some_and(|c| c.is_whitespace()) {
            chars.next();
        }
        if chars.peek() == Some(&']') {
            chars.next();
            break;
        }
        let quote = chars
            .next()
            .filter(|c| *c == '\'' || *c == '"')
            .ok_or(error)?;
        let mut path = String::new();
        loop {
            let c = chars.next().ok_or(error)?;
            if c == quote {
                break;
            }
            if c != '\\' {
                path.push(c);
                continue;
            }
            match chars.next().ok_or(error)? {
                '\\' => path.push('\\'),
                '\'' => path.push('\''),
                '"' => path.push('"'),
                'n' => path.push('\n'),
                'r' => path.push('\r'),
                't' => path.push('\t'),
                escape @ ('x' | 'u' | 'U') => {
                    let n = match escape {
                        'x' => 2,
                        'u' => 4,
                        _ => 8,
                    };
                    let digits: String = chars.by_ref().take(n).collect();
                    if digits.len() != n {
                        return Err(error.into());
                    }
                    path.push(
                        u32::from_str_radix(&digits, 16)
                            .ok()
                            .and_then(char::from_u32)
                            .ok_or(error)?,
                    );
                }
                _ => return Err(error.into()),
            }
        }
        paths.push(path);
        if paths.len() > 4096 {
            return Err(error.into());
        }
        while chars.peek().is_some_and(|c| c.is_whitespace()) {
            chars.next();
        }
        match chars.next() {
            Some(',') => {}
            Some(']') => break,
            _ => return Err(error.into()),
        }
    }
    if chars.any(|c| !c.is_whitespace()) {
        return Err(error.into());
    }
    Ok(paths)
}

/// Call under `UpdateLock`. Unknown evidence is a result, never permission to
/// delete. Only directories with a verified native marker are candidates:
/// legacy Python runtimes and malformed/incomplete directories remain intact.
pub fn remove_unused(layout: &Layout, evidence: Result<Evidence>) -> Value {
    let mut removed = Vec::new();
    let mut held = Vec::new();
    let mut kept = Vec::new();
    let evidence = match evidence {
        Ok(evidence) => evidence,
        Err(error) => {
            return json!({"removed":[],"held":[],"kept":[],"unverified":[],"error":error});
        }
    };
    let current = current_runtime(layout);
    let running = running_runtime(layout);
    for runtime in list_runtimes(layout) {
        let path = &runtime.path;
        if current
            .as_ref()
            .is_some_and(|r| r.sequence == runtime.sequence)
            || evidence
                .registrations
                .iter()
                .chain(&evidence.tunnels)
                .any(|p| same_path(p, path))
        {
            kept.push(path.clone());
            continue;
        }
        let pids: BTreeSet<_> = evidence
            .processes
            .iter()
            .filter(|(_, _, image)| {
                image.starts_with(path) || same_path(image, &runtime.executable())
            })
            .map(|p| p.0)
            .collect();
        if !pids.is_empty()
            || running
                .as_ref()
                .is_some_and(|r| r.sequence == runtime.sequence)
        {
            held.push(json!({"path":path,"processes":pids.len().max(1)}));
            continue;
        }
        let lock_path = runtime.usage_lock_path();
        let lock = match launch::usage_file(&lock_path) {
            Ok(file) => file,
            Err(_) => {
                kept.push(path.clone());
                continue;
            }
        };
        if lock.try_lock().is_err() {
            held.push(json!({"path":path,"processes":1}));
            continue;
        }
        // Recheck completion and newest selection while holding usage and
        // update locks. A launch cannot select an old directory concurrently.
        if current_runtime(layout).is_some_and(|r| r.sequence == runtime.sequence)
            || read_runtime(path).is_err()
        {
            kept.push(path.clone());
            continue;
        }
        let quarantine = layout.root.join(format!(
            "{}.removing-native-{}",
            runtime.name(),
            uuid::Uuid::new_v4()
        ));
        if fs::rename(path, &quarantine).is_err() {
            held.push(json!({"path":path,"processes":0}));
            continue;
        }
        if fs::remove_dir_all(&quarantine).is_err() {
            let restored = fs::rename(&quarantine, path).is_ok();
            held.push(json!({"path":path,"processes":0,"error":if restored {"runtime removal failed; original path restored"} else {"runtime removal failed; quarantined directory retained"}}));
        } else {
            removed.push(path.clone());
        }
    }
    // Old launcher copies may include Python launchers; never delete them as
    // part of the native migration. Reruns can safely leave these tiny files.
    json!({"removed":removed,"held":held,"kept":kept,"unverified":[],"error":null})
}
