//! `_archive_state`: a tar.gz of the data dir minus the bank and the backups.
//!
//! Members carry what Python's `tarfile` records and reads back (name, type,
//! mode, uid, gid, size, float mtime, link target, content). Container bytes
//! (header encoding, PAX layout, gzip header, padding) are not contract.
use flate2::{Compression, write::GzEncoder};
use std::{
    fs::{self, Metadata},
    io::{self, Read},
    path::{Path, PathBuf},
};
use tar::{Builder, EntryType, Header};

const EXCLUDE: [&str; 2] = ["embedded_pg", "backups"];

fn top_children(data_dir: &Path) -> Option<Vec<(String, PathBuf)>> {
    let mut children = Vec::new();
    for entry in fs::read_dir(data_dir).ok()? {
        let entry = entry.ok()?;
        let name = entry.file_name().into_string().ok()?;
        children.push((name, entry.path()));
    }
    children.sort_by_key(|(name, _)| super::path_sort_key(name));
    Some(children)
}

fn excluded(name: &str, path: &Path, bdir: Option<&Path>) -> bool {
    EXCLUDE.contains(&name)
        || bdir.is_some_and(|bdir| fs::canonicalize(path).ok() == fs::canonicalize(bdir).ok())
}

/// The file's link count; Python's `os.lstat` reports it on Windows too.
#[cfg(windows)]
#[allow(unsafe_code)]
fn link_count(path: &Path) -> Option<u32> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };
    let file = fs::File::open(path).ok()?;
    // SAFETY: an all-zero BY_HANDLE_FILE_INFORMATION is a valid plain-data value.
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: the handle stays open for the call and `info` is a valid out pointer.
    let ok = unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) };
    (ok != 0).then_some(info.nNumberOfLinks)
}

/// Every tree shape this port archives exactly; anything else defers first.
fn supported(path: &Path, meta: &Metadata) -> Option<()> {
    let kind = meta.file_type();
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        // Reparse points (symlinks, junctions, placeholders) have Windows-only stat rules.
        if meta.file_attributes() & 0x400 != 0 || kind.is_symlink() {
            return None;
        }
        // A second link to the same file would become a tar hard-link member.
        if kind.is_file() && link_count(path)? > 1 {
            return None;
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if kind.is_symlink() {
            fs::read_link(path).ok()?.to_str()?;
            return Some(());
        }
        // A second link to the same inode would become a tar hard-link member.
        if kind.is_file() && meta.nlink() > 1 {
            return None;
        }
    }
    if kind.is_file() {
        return Some(());
    }
    if !kind.is_dir() {
        return None;
    }
    for entry in fs::read_dir(path).ok()? {
        let entry = entry.ok()?;
        entry.file_name().to_str()?;
        let child = entry.path();
        supported(&child, &fs::symlink_metadata(&child).ok()?)?;
    }
    Some(())
}

/// Resolve components and symlink targets in order, including targets that
/// become reachable after mkdir creates an earlier nonexistent component.
fn resolved(path: &Path) -> Option<PathBuf> {
    fn walk(
        mut base: PathBuf,
        path: &Path,
        links: &mut std::collections::HashSet<PathBuf>,
    ) -> Option<PathBuf> {
        use std::path::Component;
        #[cfg(windows)]
        let mut crossed_reparse = false;
        for component in path.components() {
            match component {
                Component::CurDir => {}
                Component::ParentDir => {
                    #[cfg(windows)]
                    if crossed_reparse {
                        return None;
                    }
                    base.pop();
                }
                Component::Prefix(_) => base.push(component.as_os_str()),
                Component::RootDir => {
                    base.push(component.as_os_str());
                    base = fs::canonicalize(&base).ok()?;
                }
                Component::Normal(_) => {
                    base.push(component.as_os_str());
                    match fs::symlink_metadata(&base) {
                        Ok(meta) => {
                            #[cfg(windows)]
                            {
                                use std::os::windows::fs::MetadataExt;
                                crossed_reparse |= meta.file_attributes() & 0x400 != 0;
                            }
                            if meta.is_symlink() {
                                let link = base.clone();
                                if !links.insert(link.clone()) {
                                    return None;
                                }
                                let target = fs::read_link(&base).ok()?;
                                base.pop();
                                base = walk(base, &target, links)?;
                                links.remove(&link);
                            } else {
                                base = fs::canonicalize(&base).ok()?;
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                        Err(_) => return None,
                    }
                }
            }
        }
        Some(base)
    }
    #[cfg(windows)]
    // Win32 resolves ordinary dot components before following reparse points;
    // verbatim paths keep them, and a parent after a reparse point defers above.
    let absolute = std::path::absolute(path).ok()?;
    #[cfg(windows)]
    let path = absolute.as_path();
    let base = fs::canonicalize(std::env::current_dir().ok()?).ok()?;
    let result = walk(base, path, &mut std::collections::HashSet::new())?;
    result.is_absolute().then_some(result)
}

/// Admit the data dir's tree before any effect. Defers when the output dir
/// would be the data dir or sit inside an archived child (Python would
/// archive its own partial file), when a child cannot be resolved the way
/// `Path.resolve()` resolves it (a symlink loop), and for unsupported shapes.
pub(super) fn admit(data_dir: &Path, bdir: &Path) -> Option<()> {
    let out = resolved(bdir)?;
    let data = fs::canonicalize(data_dir).ok()?;
    #[cfg(windows)]
    if !out.starts_with(&data) && output_aliases_source(&data, &out)? {
        return None;
    }
    if data == out {
        return None;
    }
    // mkdir creates missing children before tar.add lists them. Containment
    // must therefore include children that do not exist at preflight time.
    if let Ok(relative) = out.strip_prefix(&data) {
        let mut parts = relative.components();
        if let Some(first) = parts.next()
            && parts.next().is_some()
            && !EXCLUDE.contains(&first.as_os_str().to_str()?)
        {
            return None;
        }
    }
    for (name, path) in top_children(data_dir)? {
        if EXCLUDE.contains(&name.as_str()) {
            continue;
        }
        let meta = fs::symlink_metadata(&path).ok()?;
        let target = match fs::canonicalize(&path) {
            Ok(target) => Some(target),
            Err(error) if error.kind() == io::ErrorKind::NotFound && meta.is_symlink() => None,
            Err(_) => return None,
        };
        if target.as_ref() == Some(&out) {
            continue;
        }
        if target
            .as_ref()
            .is_some_and(|target| out.starts_with(target))
        {
            return None;
        }
        supported(&path, &meta)?;
    }
    Some(())
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn directory_identity(path: &Path) -> Option<(u32, u64)> {
    use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, FILE_FLAG_BACKUP_SEMANTICS, GetFileInformationByHandle,
    };
    let file = fs::OpenOptions::new()
        .access_mode(0)
        .share_mode(7)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)
        .ok()?;
    // SAFETY: plain-data output is initialized, and both handle and pointer
    // remain valid for the synchronous information query.
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) } == 0 {
        return None;
    }
    Some((
        info.dwVolumeSerialNumber,
        (u64::from(info.nFileIndexHigh) << 32) | u64::from(info.nFileIndexLow),
    ))
}

#[cfg(windows)]
fn output_aliases_source(data: &Path, out: &Path) -> Option<bool> {
    let identity = directory_identity(data)?;
    let mut ancestor = out.to_path_buf();
    loop {
        match fs::metadata(&ancestor) {
            Ok(meta) if meta.is_dir() => {
                if directory_identity(&ancestor)? == identity {
                    return Some(true);
                }
            }
            Ok(_) => return None,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return None,
        }
        if !ancestor.pop() {
            return Some(false);
        }
    }
}

/// `st_mode & 0o7777` as CPython's `os.lstat` reports it.
#[cfg(unix)]
fn mode(_path: &Path, meta: &Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    meta.permissions().mode() & 0o7777
}

#[cfg(windows)]
fn mode(path: &Path, meta: &Metadata) -> u32 {
    let readonly = meta.permissions().readonly();
    if meta.is_dir() {
        return if readonly { 0o555 } else { 0o777 };
    }
    let mut mode = if readonly { 0o444 } else { 0o666 };
    // CPython compares everything after the path's last '.', dotfiles included.
    let text = path.to_string_lossy();
    let executable = text.rfind('.').is_some_and(|dot| {
        [".exe", ".bat", ".cmd", ".com"]
            .iter()
            .any(|ext| text[dot..].eq_ignore_ascii_case(ext))
    });
    if executable {
        mode |= 0o111;
    }
    mode
}

#[cfg(unix)]
fn owner(meta: &Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;
    (u64::from(meta.uid()), u64::from(meta.gid()))
}

#[cfg(windows)]
fn owner(_meta: &Metadata) -> (u64, u64) {
    (0, 0)
}

/// tarfile resolves uid/gid with pwd/grp on Unix and leaves both names empty
/// when the id has no NSS record; those modules are absent on Windows.
#[cfg(unix)]
#[allow(unsafe_code)]
fn owner_name(id: u32, group: bool) -> io::Result<Vec<u8>> {
    use std::{ffi::CStr, ptr};
    let mut buffer = vec![0u8; 1024];
    loop {
        // SAFETY: both structs are plain C data, initialized by the reentrant
        // NSS calls; the result pointer and name point into this live buffer.
        let (code, name) = unsafe {
            if group {
                let mut record: libc::group = std::mem::zeroed();
                let mut result = ptr::null_mut();
                let code = libc::getgrgid_r(
                    id,
                    &mut record,
                    buffer.as_mut_ptr().cast(),
                    buffer.len(),
                    &mut result,
                );
                (
                    code,
                    if result.is_null() {
                        ptr::null()
                    } else {
                        record.gr_name as *const libc::c_char
                    },
                )
            } else {
                let mut record: libc::passwd = std::mem::zeroed();
                let mut result = ptr::null_mut();
                let code = libc::getpwuid_r(
                    id,
                    &mut record,
                    buffer.as_mut_ptr().cast(),
                    buffer.len(),
                    &mut result,
                );
                (
                    code,
                    if result.is_null() {
                        ptr::null()
                    } else {
                        record.pw_name as *const libc::c_char
                    },
                )
            }
        };
        if code == libc::ERANGE {
            buffer.resize(buffer.len() * 2, 0);
            continue;
        }
        // pwd/grp report an unsuccessful lookup as KeyError; tarfile keeps
        // numeric ownership and leaves the corresponding name empty.
        if code != 0 || name.is_null() {
            return Ok(Vec::new());
        }
        // SAFETY: successful NSS call returned a NUL-terminated name in buffer.
        return Ok(unsafe { CStr::from_ptr(name) }.to_bytes().to_vec());
    }
}

fn owner_names(uid: u64, gid: u64) -> io::Result<(Vec<u8>, Vec<u8>)> {
    #[cfg(unix)]
    {
        Ok((
            owner_name(uid as u32, false)?,
            owner_name(gid as u32, true)?,
        ))
    }
    #[cfg(windows)]
    {
        let _ = (uid, gid);
        Ok((Vec::new(), Vec::new()))
    }
}

/// Python `str(float)`: shortest round-trip digits, `.0` on integral values.
fn float_text(value: f64) -> io::Result<String> {
    crate::float_repr::finite_float(value)
}

fn pax_record(key: &str, value: &[u8]) -> Vec<u8> {
    let mut payload = format!(" {key}=").into_bytes();
    payload.extend_from_slice(value);
    payload.push(b'\n');
    let mut length = payload.len() + 1;
    while length != payload.len() + length.to_string().len() {
        length = payload.len() + length.to_string().len();
    }
    let mut record = length.to_string().into_bytes();
    record.extend(payload);
    record
}

fn ascii_field(field: &mut [u8], text: &str) {
    for (slot, character) in field.iter_mut().zip(text.chars()) {
        *slot = if character.is_ascii() {
            character as u8
        } else {
            b'?'
        };
    }
}

/// Exactly the stat size, as `tarfile.addfile` copies it: a file that grew is
/// cut at its stat size, one that shrank fails the archive.
struct Exact {
    file: io::Take<fs::File>,
    remaining: u64,
}

impl Read for Exact {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let read = self.file.read(buffer)?;
        if read == 0 && self.remaining > 0 && !buffer.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "unexpected end of data",
            ));
        }
        self.remaining -= read as u64;
        Ok(read)
    }
}

struct Member<'a> {
    name: String,
    kind: EntryType,
    meta: &'a Metadata,
    path: &'a Path,
    link: Option<String>,
}

fn append<W: io::Write>(tar: &mut Builder<W>, member: Member<'_>) -> io::Result<()> {
    let size = if member.kind == EntryType::Regular {
        member.meta.len()
    } else {
        0
    };
    let mtime = super::seconds(member.meta.modified()?);
    let (uid, gid) = owner(member.meta);
    let (uname, gname) = owner_names(uid, gid)?;
    let mut records = Vec::new();
    if std::str::from_utf8(&uname).is_err() || std::str::from_utf8(&gname).is_err() {
        records.extend(pax_record("hdrcharset", b"BINARY"));
    }
    for (key, value) in [("uname", &uname), ("gname", &gname)] {
        if !value.is_ascii() || value.len() > 32 {
            records.extend(pax_record(key, value));
        }
    }
    if !member.name.is_ascii() || member.name.len() > 100 {
        records.extend(pax_record("path", member.name.as_bytes()));
    }
    if let Some(link) = &member.link
        && (!link.is_ascii() || link.len() > 100)
    {
        records.extend(pax_record("linkpath", link.as_bytes()));
    }
    records.extend(pax_record("mtime", float_text(mtime)?.as_bytes()));
    if !records.is_empty() {
        let mut pax = Header::new_ustar();
        ascii_field(&mut pax.as_old_mut().name, "././@PaxHeader");
        pax.set_entry_type(EntryType::XHeader);
        pax.set_mode(0o644);
        pax.set_size(records.len() as u64);
        pax.set_cksum();
        tar.append(&pax, records.as_slice())?;
    }
    let mut header = Header::new_ustar();
    ascii_field(&mut header.as_old_mut().name, &member.name);
    if let Some(link) = &member.link {
        ascii_field(&mut header.as_old_mut().linkname, link);
    }
    header.set_entry_type(member.kind);
    header.set_mode(mode(member.path, member.meta));
    header.set_uid(uid);
    header.set_gid(gid);
    let ustar = header.as_ustar_mut().expect("ustar header");
    ascii_field(&mut ustar.uname, &String::from_utf8_lossy(&uname));
    ascii_field(&mut ustar.gname, &String::from_utf8_lossy(&gname));
    header.set_size(size);
    header.set_mtime(mtime.round_ties_even().max(0.0) as u64);
    header.set_cksum();
    if member.kind == EntryType::Regular {
        let file = fs::File::open(member.path)?.take(size);
        tar.append(
            &header,
            Exact {
                file,
                remaining: size,
            },
        )
    } else {
        tar.append(&header, io::empty())
    }
}

/// `tarfile.add(path, arcname, recursive=True)`.
fn add<W: io::Write>(tar: &mut Builder<W>, path: &Path, arcname: &str) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    let kind = meta.file_type();
    if kind.is_symlink() {
        let link = fs::read_link(path)?.to_string_lossy().into_owned();
        let name = arcname.to_owned();
        let link = Some(link);
        return append(
            tar,
            Member {
                name,
                kind: EntryType::Symlink,
                meta: &meta,
                path,
                link,
            },
        );
    }
    if kind.is_dir() {
        let member = Member {
            name: format!("{arcname}/"),
            kind: EntryType::Directory,
            meta: &meta,
            path,
            link: None,
        };
        append(tar, member)?;
        let mut names = Vec::new();
        for entry in fs::read_dir(path)? {
            names.push(entry?.file_name().to_string_lossy().into_owned());
        }
        // os.listdir names sorted as str: code-point order on every platform.
        names.sort();
        for name in names {
            add(tar, &path.join(&name), &format!("{arcname}/{name}"))?;
        }
        return Ok(());
    }
    let member = Member {
        name: arcname.to_owned(),
        kind: EntryType::Regular,
        meta: &meta,
        path,
        link: None,
    };
    append(tar, member)
}

pub(super) fn write(data_dir: &Path, bdir: &Path, ts: &str) -> io::Result<PathBuf> {
    let target = super::args::join(bdir, &format!("{}{ts}.tar.gz", super::STATE_PREFIX));
    let partial = super::args::join(bdir, &format!("{}{ts}.tar.gz.part", super::STATE_PREFIX));
    let written = (|| -> io::Result<()> {
        let gz = GzEncoder::new(fs::File::create(&partial)?, Compression::best());
        let mut tar = Builder::new(gz);
        let children = top_children(data_dir)
            .ok_or_else(|| io::Error::other("data dir could not be listed"))?;
        for (name, path) in children {
            if excluded(&name, &path, Some(bdir)) {
                continue;
            }
            add(&mut tar, &path, &name)?;
        }
        tar.into_inner()?.finish()?;
        Ok(())
    })();
    if let Err(error) = written {
        let _ = fs::remove_file(&partial);
        return Err(error);
    }
    fs::rename(&partial, &target)?;
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn unassigned_owner_ids_have_empty_names() {
        assert_eq!(
            owner_names(u64::from(u32::MAX), u64::from(u32::MAX)).unwrap(),
            (Vec::new(), Vec::new())
        );
    }

    #[test]
    fn python_float_shortest_ties() {
        // Python writer: pseudolife_memory/backup_cli.py:150 (tar.add),
        // CPython tarfile.TarInfo.create_pax_header (pax_headers[name] = str(val)).
        let mut wrong = Vec::new();
        for (bits, expected) in [
            (0x430c6bf526340002, "1000000000000000.2"),
            (0xc308130f222a4572, "-847044394961070.2"),
            (0x42d526daef896bc8, "93026504287663.12"),
        ] {
            let actual = float_text(f64::from_bits(bits)).unwrap();
            if actual != expected {
                wrong.push(format!("{bits:016x}: {actual} != {expected}"));
            }
        }
        assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    }

    #[test]
    fn python_float_full_cpython_table() {
        let table = include_str!("../../../tests/cli_audit_float_repr.tsv");
        let mut count = 0;
        for line in table
            .lines()
            .filter(|line| !line.starts_with('#') && !line.is_empty())
        {
            let (bits, expected) = line.split_once('\t').unwrap();
            let value = f64::from_bits(u64::from_str_radix(bits, 16).unwrap());
            assert_eq!(float_text(value).unwrap(), expected, "bits {bits}");
            count += 1;
        }
        assert_eq!(count, 5358);
        assert!(float_text(f64::INFINITY).is_err());
        assert!(float_text(f64::NAN).is_err());
    }
}
