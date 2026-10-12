//! `runtimes.list_processes` and Python's `sys.stdin.isatty()`.
use std::path::PathBuf;

/// `(pid, parent pid, image)`; `Ok(None)` where the platform has no table,
/// `Err(())` where Python raises OSError.
pub(super) type Table = Vec<(u32, u32, PathBuf)>;

pub(super) fn list() -> Result<Option<Table>, ()> {
    #[cfg(windows)]
    {
        windows::processes().map(Some)
    }
    #[cfg(target_os = "linux")]
    {
        proc_processes().map(Some)
    }
    #[cfg(target_os = "macos")]
    {
        ps_processes().map(Some)
    }
    #[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
    {
        Ok(None)
    }
}

/// `os.getppid()`.
pub(super) fn parent_pid(_table: &Table) -> Option<u32> {
    #[cfg(unix)]
    {
        Some(std::os::unix::process::parent_id())
    }
    #[cfg(windows)]
    {
        let own = std::process::id();
        _table
            .iter()
            .find(|(pid, _, _)| *pid == own)
            .map(|(_, ppid, _)| *ppid)
    }
}

/// `sys.stdin.isatty()`: on Windows any character device (the CRT's
/// `_isatty`), so `NUL` counts as one.
pub(super) fn stdin_is_tty() -> bool {
    #[cfg(windows)]
    {
        windows::stdin_is_character_device()
    }
    #[cfg(not(windows))]
    {
        use std::io::IsTerminal;
        std::io::stdin().is_terminal()
    }
}

#[cfg(target_os = "linux")]
fn proc_processes() -> Result<Table, ()> {
    let mut rows = Vec::new();
    let names = std::fs::read_dir("/proc").map_err(|_| ())?;
    for entry in names.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Ok(pid) = name.parse::<u32>() else {
            continue;
        };
        let base = PathBuf::from("/proc").join(name);
        let Ok(stat) = std::fs::read(base.join("stat")) else {
            continue;
        };
        let stat = String::from_utf8_lossy(&stat);
        let Some(ppid) = stat
            .rsplit_once(')')
            .and_then(|(_, rest)| rest.split_whitespace().nth(1))
            .and_then(|value| value.parse::<u32>().ok())
        else {
            continue;
        };
        let Ok(cmdline) = std::fs::read(base.join("cmdline")) else {
            continue;
        };
        let first = cmdline.split(|b| *b == 0).next().unwrap_or_default();
        let first = String::from_utf8_lossy(first).into_owned();
        let image = if first.starts_with('/') {
            PathBuf::from(first)
        } else {
            match std::fs::read_link(base.join("exe")) {
                Ok(target) => target,
                Err(_) => continue,
            }
        };
        rows.push((pid, ppid, image));
    }
    if rows.is_empty() {
        return Err(());
    }
    Ok(rows)
}

#[cfg(target_os = "macos")]
fn ps_processes() -> Result<Table, ()> {
    let output = std::process::Command::new("ps")
        .args(["-axo", "pid=,ppid=,args="])
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|_| ())?;
    if !output.status.success() {
        return Err(());
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let mut rows = Vec::new();
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        let (Some(pid), Some(ppid), Some(first)) = (parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        let (Ok(pid), Ok(ppid)) = (pid.parse::<u32>(), ppid.parse::<u32>()) else {
            continue;
        };
        rows.push((pid, ppid, PathBuf::from(first)));
    }
    Ok(rows)
}

// Native process inspection is confined to this module, as the credential
// and doorbell modules confine theirs.
#[cfg(windows)]
#[allow(unsafe_code)]
mod windows {
    use super::Table;
    use std::{ffi::OsString, os::windows::ffi::OsStringExt, os::windows::io::AsRawHandle};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, INVALID_HANDLE_VALUE},
        Storage::FileSystem::{FILE_TYPE_CHAR, GetFileType},
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
                TH32CS_SNAPPROCESS,
            },
            Threading::{
                OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW,
            },
        },
    };

    pub(super) fn processes() -> Result<Table, ()> {
        let mut parents = Vec::new();
        // SAFETY: the snapshot handle is checked and closed exactly once; the
        // entry is a zeroed PROCESSENTRY32W whose dwSize names its own size.
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if snapshot.is_null() || snapshot == INVALID_HANDLE_VALUE {
                return Err(());
            }
            let mut entry: PROCESSENTRY32W = std::mem::zeroed();
            entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
            let mut more = Process32FirstW(snapshot, &mut entry);
            while more != 0 {
                parents.push((entry.th32ProcessID, entry.th32ParentProcessID));
                more = Process32NextW(snapshot, &mut entry);
            }
            CloseHandle(snapshot);
        }
        if parents.is_empty() {
            return Err(());
        }
        let mut rows = Vec::new();
        let mut image = vec![0u16; 32768];
        for (pid, ppid) in parents {
            // SAFETY: a null handle is skipped; an opened one is closed once.
            // The buffer's length is passed in and the written length read back.
            unsafe {
                let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if handle.is_null() {
                    continue;
                }
                let mut size = image.len() as u32;
                if QueryFullProcessImageNameW(handle, 0, image.as_mut_ptr(), &mut size) != 0 {
                    let path = OsString::from_wide(&image[..size as usize]);
                    rows.push((pid, ppid, path.into()));
                }
                CloseHandle(handle);
            }
        }
        Ok(rows)
    }

    pub(super) fn stdin_is_character_device() -> bool {
        let handle = std::io::stdin().as_raw_handle();
        if handle.is_null() {
            return false;
        }
        // SAFETY: GetFileType only inspects the borrowed standard handle.
        unsafe { GetFileType(handle) == FILE_TYPE_CHAR }
    }
}
