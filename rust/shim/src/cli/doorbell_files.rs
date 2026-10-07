//! The leaf's private UTF-8 records and first-byte Windows lock.
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::Path,
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, ()>;
fn lexical_absolute(path: &Path) -> Result<std::path::PathBuf> {
    if path.is_absolute() {
        return Ok(path.to_path_buf());
    }
    #[cfg(windows)]
    {
        use std::path::Component;
        let append = |base: &Path, tail: &Path| {
            // PathBuf::join normalizes .. for verbatim Windows prefixes.
            // Append raw path text so even those ancestors remain inspectable.
            let mut text = base.as_os_str().to_os_string();
            if !text.to_string_lossy().ends_with(['/', '\\']) {
                text.push("\\");
            }
            text.push(tail.as_os_str());
            std::path::PathBuf::from(text)
        };
        let mut components = path.components();
        if let Some(Component::Prefix(prefix)) = components.next() {
            // Resolve only the drive's cwd; keep the supplied .. components
            // visible to the ancestry check, as Python Path.absolute does.
            let base = std::path::absolute(Path::new(prefix.as_os_str())).map_err(|_| ())?;
            return Ok(append(&base, components.as_path()));
        }
        let cwd = std::env::current_dir().map_err(|_| ())?;
        if path.has_root() {
            let prefix = cwd.components().next().ok_or(())?;
            let mut text = prefix.as_os_str().to_os_string();
            text.push(path.as_os_str());
            return Ok(std::path::PathBuf::from(text));
        }
        Ok(append(&cwd, path))
    }
    #[cfg(not(windows))]
    {
        Ok(std::env::current_dir().map_err(|_| ())?.join(path))
    }
}
fn no_symlink(path: &Path) -> Result<()> {
    let absolute = lexical_absolute(path)?;
    for ancestor in absolute.ancestors() {
        if fs::symlink_metadata(ancestor).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err(());
        }
    }
    Ok(())
}
pub(super) fn prepare_dir(path: &Path) -> Result<()> {
    no_symlink(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).map_err(|_| ())?;
        }
        let created = fs::DirBuilder::new().mode(0o700).create(path);
        if created.is_err() && !path.is_dir() {
            return Err(());
        }
    }
    #[cfg(windows)]
    {
        fs::create_dir_all(path).map_err(|_| ())?;
    }
    no_symlink(path)?;
    if !path.is_dir() {
        return Err(());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).map_err(|_| ())?;
    }
    Ok(())
}
fn open(path: &Path, write: bool, create: bool) -> Result<File> {
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
        return Err(());
    }
    let options = |new| {
        let mut o = OpenOptions::new();
        o.read(true).write(write).create_new(new);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            o.mode(0o600).custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            );
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            o.share_mode(3);
            if new {
                o.access_mode(0x0012019f | 0x000c0000);
            }
        }
        o
    };
    let (file, new) = if create {
        match options(true).open(path) {
            Ok(f) => (f, true),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                (options(false).open(path).map_err(|_| ())?, false)
            }
            Err(_) => return Err(()),
        }
    } else {
        (options(false).open(path).map_err(|_| ())?, false)
    };
    let m = file.metadata().map_err(|_| ())?;
    if !m.is_file() {
        return Err(());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if m.nlink() != 1 {
            return Err(());
        }
        if new {
            file.set_permissions(fs::Permissions::from_mode(0o600))
                .map_err(|_| ())?;
        }
        if m.uid() != rustix::process::geteuid().as_raw() || m.mode() & 0o077 != 0 {
            return Err(());
        }
    }
    #[cfg(windows)]
    {
        if new {
            crate::credentials::windows_security::make_private(&file).map_err(|_| ())?;
        }
        crate::credentials::windows_security::validate(&file).map_err(|_| ())?;
    }
    Ok(file)
}
pub(super) fn read_text(path: &Path) -> Result<String> {
    // A Unicode codepoint bound follows universal-newline decoding; this is
    // intentionally different from the board helper's byte bound.
    let file = open(path, false, false)?;
    let mut reader = std::io::BufReader::new(file);
    let mut output = String::new();
    let mut previous_cr = false;
    let mut characters = 0;
    loop {
        let mut first = [0];
        let n = reader.read(&mut first).map_err(|_| ())?;
        if n == 0 {
            break;
        }
        let width = match first[0] {
            0..=127 => 1,
            194..=223 => 2,
            224..=239 => 3,
            240..=244 => 4,
            _ => return Err(()),
        };
        let mut bytes = [0; 4];
        bytes[0] = first[0];
        reader.read_exact(&mut bytes[1..width]).map_err(|_| ())?;
        let c = std::str::from_utf8(&bytes[..width])
            .map_err(|_| ())?
            .chars()
            .next()
            .ok_or(())?;
        if previous_cr && c == '\n' {
            previous_cr = false;
            continue;
        }
        previous_cr = c == '\r';
        output.push(if previous_cr { '\n' } else { c });
        characters += 1;
        if characters > 8192 {
            return Err(());
        }
    }
    Ok(output)
}
pub(super) fn write_atomic(path: &Path, text: &str) -> Result<()> {
    if fs::symlink_metadata(path).is_ok() {
        read_text(path)?;
    }
    let parent = path.parent().ok_or(())?;
    let temporary = parent.join(format!(".bell-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut file = open(&temporary, true, true)?;
        file.write_all(text.as_bytes())
            .and_then(|_| file.write_all(b"\n"))
            .and_then(|_| file.sync_all())
            .map_err(|_| ())?;
        drop(file);
        fs::rename(&temporary, path).map_err(|_| ())?;
        #[cfg(unix)]
        {
            if let Err(e) = File::open(parent).and_then(|f| f.sync_all())
                && !matches!(e.raw_os_error(), Some(libc::EINVAL | libc::ENOTSUP))
            {
                return Err(());
            }
        }
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result
}
pub(super) struct Lock(File);
impl Drop for Lock {
    fn drop(&mut self) {
        unlock(&self.0);
    }
}
pub(super) fn lock(path: &Path) -> Result<Lock> {
    let mut file = open(path, true, true)?;
    if file.metadata().map_err(|_| ())?.len() == 0 {
        file.write_all(b"0")
            .and_then(|_| file.flush())
            .map_err(|_| ())?;
    }
    let deadline = Instant::now() + Duration::from_millis(250);
    loop {
        if try_lock(&file)? {
            return Ok(Lock(file));
        }
        let now = Instant::now();
        if now >= deadline {
            return Err(());
        }
        std::thread::sleep(Duration::from_millis(10).min(deadline - now));
    }
}
#[cfg(unix)]
fn try_lock(file: &File) -> Result<bool> {
    match rustix::fs::flock(file, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
        Ok(()) => Ok(true),
        Err(rustix::io::Errno::ACCESS | rustix::io::Errno::AGAIN) => Ok(false),
        Err(_) => Err(()),
    }
}
#[cfg(unix)]
fn unlock(file: &File) {
    let _ = rustix::fs::flock(file, rustix::fs::FlockOperation::Unlock);
}
#[cfg(windows)]
#[allow(unsafe_code)]
fn try_lock(file: &File) -> Result<bool> {
    use std::os::windows::io::AsRawHandle;
    // SAFETY: a live borrowed File handle, locking exactly byte zero; no
    // pointer is retained by the synchronous Win32 operation.
    if unsafe {
        windows_sys::Win32::Storage::FileSystem::LockFile(file.as_raw_handle() as _, 0, 0, 1, 0)
    } != 0
    {
        return Ok(true);
    }
    match std::io::Error::last_os_error().raw_os_error() {
        Some(33 | 5) => Ok(false),
        _ => Err(()),
    }
}
#[cfg(windows)]
#[allow(unsafe_code)]
fn unlock(file: &File) {
    use std::os::windows::io::AsRawHandle;
    // SAFETY: the handle remains live; these coordinates match try_lock.
    unsafe {
        windows_sys::Win32::Storage::FileSystem::UnlockFile(file.as_raw_handle() as _, 0, 0, 1, 0);
    }
}

#[cfg(test)]
mod fable125_path_controls {
    use super::*;

    #[test]
    fn lexical_absolute_retains_parent_components() {
        let relative = Path::new("fable125-link/../digests");
        let absolute = lexical_absolute(relative).unwrap();
        assert!(absolute.is_absolute());
        assert!(absolute.ancestors().any(|p| p.ends_with("fable125-link")));
        assert_eq!(lexical_absolute(&absolute).unwrap(), absolute);
    }

    #[cfg(windows)]
    #[test]
    fn drive_relative_and_root_relative_keep_parent_components() {
        let cwd = std::env::current_dir().unwrap();
        let prefix = cwd.components().next().unwrap();
        let drive_relative = Path::new(prefix.as_os_str()).join("fable125-link/../digests");
        let rooted = Path::new("\\fable125-link\\..\\digests");
        for path in [drive_relative.as_path(), rooted] {
            let absolute = lexical_absolute(path).unwrap();
            assert!(absolute.is_absolute());
            assert!(absolute.ancestors().any(|p| p.ends_with("fable125-link")));
        }
    }
}
