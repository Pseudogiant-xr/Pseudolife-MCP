//! `_archive_state`: a tar.gz of the data dir minus the bank and the backups.
//!
//! Members carry what Python's `tarfile` records and reads back (name, type,
//! mode, uid, gid, size, float mtime, link target, content). Container bytes
//! (header encoding, PAX layout, gzip header, padding) are not contract.
use flate2::{Compression, write::GzEncoder};
use std::{
    collections::{HashMap, HashSet},
    ffi::{OsStr, OsString},
    fs::{self, Metadata},
    io::{self, Read},
    path::{Path, PathBuf},
};
use tar::{Builder, EntryType, Header};

const EXCLUDE: [&str; 2] = ["embedded_pg", "backups"];

/// Preserve Python's filesystem surrogateescape bytes in PAX names.
#[cfg(unix)]
fn name_bytes(name: &OsStr) -> Vec<u8> {
    use std::os::unix::ffi::OsStrExt;
    name.as_bytes().to_vec()
}

#[cfg(windows)]
fn name_bytes(name: &OsStr) -> Vec<u8> {
    use std::os::windows::ffi::OsStrExt;
    let mut bytes = Vec::new();
    for character in char::decode_utf16(name.encode_wide()) {
        match character {
            Ok(character) => {
                let mut buffer = [0; 4];
                bytes.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
            }
            Err(error) => {
                let surrogate = error.unpaired_surrogate();
                if (0xdc80..=0xdcff).contains(&surrogate) {
                    bytes.push((surrogate - 0xdc00) as u8);
                } else {
                    // Python's tarfile cannot encode these filesystem surrogates either.
                    bytes.extend_from_slice(&[
                        0xed,
                        0xa0 | ((surrogate >> 6) & 15) as u8,
                        0x80 | (surrogate & 63) as u8,
                    ]);
                }
            }
        }
    }
    bytes
}

fn encodable(name: &OsStr) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let points = char::decode_utf16(name.encode_wide())
            .map(|character| match character {
                Ok(character) => Some(u32::from(character)),
                Err(error) if (0xdc80..=0xdcff).contains(&error.unpaired_surrogate()) => {
                    Some(u32::from(error.unpaired_surrogate()))
                }
                Err(_) => None,
            })
            .collect::<Option<Vec<_>>>();
        points.is_some_and(|points| codepoints(&name_bytes(name)) == points)
    }
    #[cfg(unix)]
    {
        let _ = name;
        true
    }
}

/// Python sorts str code points, including U+DC80..U+DCFF from invalid UTF-8.
fn codepoints(bytes: &[u8]) -> Vec<u32> {
    let mut points = Vec::new();
    let mut rest = bytes;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(text) => {
                points.extend(text.chars().map(u32::from));
                break;
            }
            Err(error) => {
                let valid = error.valid_up_to();
                points.extend(
                    std::str::from_utf8(&rest[..valid])
                        .unwrap()
                        .chars()
                        .map(u32::from),
                );
                let end = valid + error.error_len().unwrap_or(rest.len() - valid);
                points.extend(
                    rest[valid..end]
                        .iter()
                        .map(|byte| 0xdc00 + u32::from(*byte)),
                );
                rest = &rest[end..];
            }
        }
    }
    points
}

fn name_sort_key(name: &OsStr, fold: bool) -> Vec<u32> {
    let points = codepoints(&name_bytes(name));
    if !fold {
        return points;
    }
    let mut folded = Vec::new();
    let mut text = String::new();
    for point in points {
        if let Some(character) = char::from_u32(point) {
            text.push(character);
        } else {
            folded.extend(text.to_lowercase().chars().map(u32::from));
            text.clear();
            folded.push(point);
        }
    }
    folded.extend(text.to_lowercase().chars().map(u32::from));
    folded
}

fn children(path: &Path, top: bool) -> io::Result<Vec<(OsString, PathBuf)>> {
    let mut children = fs::read_dir(path)?
        .map(|entry| {
            let entry = entry?;
            let name = entry.file_name();
            if !encodable(&name) {
                return Err(io::Error::other("unencodable archive name"));
            }
            Ok((name, entry.path()))
        })
        .collect::<io::Result<Vec<_>>>()?;
    children.sort_by_key(|(name, _)| name_sort_key(name, top && cfg!(windows)));
    Ok(children)
}

fn top_children(data_dir: &Path) -> Option<Vec<(OsString, PathBuf)>> {
    children(data_dir, true).ok()
}

fn is_excluded(name: &OsStr) -> bool {
    EXCLUDE.iter().any(|excluded| name == OsStr::new(excluded))
}

fn excluded(name: &OsStr, path: &Path, bdir: Option<&Path>) -> bool {
    is_excluded(name)
        || bdir.is_some_and(|bdir| fs::canonicalize(path).ok() == fs::canonicalize(bdir).ok())
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn reparse_tag(path: &Path) -> Option<u32> {
    use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_TAG_INFO, FileAttributeTagInfo, GetFileInformationByHandleEx,
    };
    let file = fs::OpenOptions::new()
        .access_mode(0)
        .share_mode(7)
        .custom_flags(0x00200000 | 0x02000000)
        .open(path)
        .ok()?;
    // SAFETY: the structure contains only integer fields, whose zero values are valid.
    let mut info: FILE_ATTRIBUTE_TAG_INFO = unsafe { std::mem::zeroed() };
    // SAFETY: the handle is open and the correctly sized output buffer is valid.
    let ok = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle() as _,
            FileAttributeTagInfo,
            (&mut info as *mut FILE_ATTRIBUTE_TAG_INFO).cast(),
            std::mem::size_of_val(&info) as u32,
        )
    };
    (ok != 0).then_some(info.ReparseTag)
}

/// Decode CPython's os.readlink substitute name, retaining its verbatim prefix.
#[cfg(any(windows, test))]
fn substitute_name(data: &[u8]) -> io::Result<Vec<u16>> {
    let invalid = || io::Error::new(io::ErrorKind::InvalidData, "invalid symlink reparse data");
    if data.len() < 20 || data[..4] != 0xa000000cu32.to_le_bytes() {
        return Err(invalid());
    }
    let offset = usize::from(u16::from_le_bytes([data[8], data[9]]));
    let length = usize::from(u16::from_le_bytes([data[10], data[11]]));
    if offset % 2 != 0 || length % 2 != 0 {
        return Err(invalid());
    }
    let start = 20usize.checked_add(offset).ok_or_else(invalid)?;
    let end = start.checked_add(length).ok_or_else(invalid)?;
    let mut name = data
        .get(start..end)
        .ok_or_else(invalid)?
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect::<Vec<_>>();
    if name.len() > 4 && name.starts_with(&[92, 63, 63, 92]) {
        name[1] = 92;
    }
    Ok(name)
}

#[cfg(unix)]
fn read_link(path: &Path) -> io::Result<PathBuf> {
    fs::read_link(path)
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn read_link(path: &Path) -> io::Result<PathBuf> {
    use std::os::windows::{ffi::OsStringExt, fs::OpenOptionsExt, io::AsRawHandle};
    use windows_sys::Win32::System::IO::DeviceIoControl;
    let file = fs::OpenOptions::new()
        .access_mode(0)
        .share_mode(0)
        .custom_flags(0x00200000 | 0x02000000)
        .open(path)?;
    let mut data = vec![0u8; 16 * 1024];
    let mut returned = 0;
    // SAFETY: the handle and output buffers remain live for this synchronous call.
    let ok = unsafe {
        DeviceIoControl(
            file.as_raw_handle() as _,
            0x000900a8,
            std::ptr::null(),
            0,
            data.as_mut_ptr().cast(),
            data.len() as u32,
            &mut returned,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    data.truncate(returned as usize);
    Ok(PathBuf::from(OsString::from_wide(&substitute_name(&data)?)))
}

fn is_link(path: &Path, meta: &Metadata) -> Option<bool> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return Some(reparse_tag(path)? == 0xa000000c);
        }
    }
    let _ = path;
    Some(meta.file_type().is_symlink())
}

fn metadata(path: &Path) -> io::Result<Metadata> {
    let meta = fs::symlink_metadata(path)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            let tag = reparse_tag(path).ok_or_else(io::Error::last_os_error)?;
            if tag & 0x20000000 == 0 {
                return match fs::metadata(path) {
                    // CPython falls back to the reparse object's stat for an unhandled tag.
                    Err(error) if error.raw_os_error() == Some(1920) => Ok(meta),
                    result => result,
                };
            }
        }
    }
    Ok(meta)
}

fn is_regular(meta: &Metadata) -> bool {
    #[cfg(windows)]
    {
        !is_directory(meta)
    }
    #[cfg(unix)]
    {
        meta.is_file()
    }
}

fn is_directory(meta: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.file_attributes() & 0x10 != 0
    }
    #[cfg(unix)]
    {
        meta.is_dir()
    }
}

#[cfg(any(windows, test))]
fn directory_link_target_supported(absolute: bool, wide_len: usize) -> bool {
    // CPython's _check_dirW includes the terminator in its MAX_PATH buffer.
    absolute && wide_len < 260
}

/// Directory reparse points are traversed by Python; keep that traversal in the tree.
fn supported(
    path: &Path,
    meta: &Metadata,
    root: &Path,
    out: &Path,
    active: &mut HashSet<PathBuf>,
    #[cfg(windows)] output_ancestors: &HashSet<(u32, u64)>,
) -> Option<()> {
    if is_link(path, meta)? {
        let link = read_link(path).ok()?;
        if !encodable(link.as_os_str()) {
            return None;
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStrExt;
            // Python's extractor infers the directory flag from the target;
            // it does not store that flag in a symlink member.
            let directory = is_directory(meta);
            let target_directory = match fs::metadata(path) {
                Ok(target) => is_directory(&target),
                Err(error) if error.kind() == io::ErrorKind::NotFound => false,
                Err(_) => return None,
            };
            if directory != target_directory
                || directory
                    && !directory_link_target_supported(
                        link.is_absolute(),
                        link.as_os_str().encode_wide().count(),
                    )
            {
                return None;
            }
        }
        return Some(());
    }
    if !is_directory(meta) {
        return Some(());
    }
    #[cfg(windows)]
    if !output_ancestors.is_empty() && output_ancestors.contains(&directory_identity(path)?) {
        return None;
    }
    let target = fs::canonicalize(path).ok()?;
    if !target.starts_with(root) || out.starts_with(&target) || !active.insert(target.clone()) {
        return None;
    }
    for (_, child) in children(path, false).ok()? {
        supported(
            &child,
            &metadata(&child).ok()?,
            root,
            out,
            active,
            #[cfg(windows)]
            output_ancestors,
        )?;
    }
    active.remove(&target);
    Some(())
}

#[cfg(unix)]
fn identity(_path: &Path, meta: &Metadata) -> io::Result<((u64, u64), u64)> {
    use std::os::unix::fs::MetadataExt;
    Ok(((meta.dev(), meta.ino()), meta.nlink()))
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn identity(path: &Path, _meta: &Metadata) -> io::Result<((u64, u64), u64)> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };
    let file = fs::File::open(path)?;
    // SAFETY: the structure contains only integer fields, whose zero values are valid.
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: the open handle and output pointer remain valid for this call.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut info) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok((
        (
            u64::from(info.dwVolumeSerialNumber),
            (u64::from(info.nFileIndexHigh) << 32) | u64::from(info.nFileIndexLow),
        ),
        u64::from(info.nNumberOfLinks),
    ))
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
    let output_ancestors = if out.starts_with(&data) {
        std::collections::HashSet::new()
    } else {
        output_ancestor_identities(&out)?
    };
    #[cfg(windows)]
    if !output_ancestors.is_empty() && output_ancestors.contains(&directory_identity(&data)?) {
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
        if is_excluded(&name) {
            continue;
        }
        let meta = metadata(&path).ok()?;
        let target = match fs::canonicalize(&path) {
            Ok(target) => Some(target),
            Err(error) if error.kind() == io::ErrorKind::NotFound && meta.is_symlink() => None,
            Err(_) => return None,
        };
        if target.as_ref() == Some(&out) {
            continue;
        }
        if !is_link(&path, &meta)?
            && target
                .as_ref()
                .is_some_and(|target| out.starts_with(target))
        {
            return None;
        }
        supported(
            &path,
            &meta,
            &data,
            &out,
            &mut HashSet::new(),
            #[cfg(windows)]
            &output_ancestors,
        )?;
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
fn output_ancestor_identities(out: &Path) -> Option<std::collections::HashSet<(u32, u64)>> {
    let mut identities = std::collections::HashSet::new();
    let mut ancestor = out.to_path_buf();
    loop {
        match fs::metadata(&ancestor) {
            Ok(meta) if meta.is_dir() => {
                identities.insert(directory_identity(&ancestor)?);
            }
            Ok(_) => return None,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return None,
        }
        if !ancestor.pop() {
            return Some(identities);
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
    if is_directory(meta) {
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

fn ascii_field(field: &mut [u8], text: &[u8]) {
    for (slot, byte) in field.iter_mut().zip(text) {
        *slot = if byte.is_ascii() { *byte } else { b'?' };
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
    name: Vec<u8>,
    kind: EntryType,
    meta: &'a Metadata,
    path: &'a Path,
    link: Option<Vec<u8>>,
}

#[cfg(unix)]
#[allow(clippy::unnecessary_cast)] // libc's dev_t and major/minor widths vary across Unix.
fn device_numbers(meta: &Metadata) -> (u32, u32) {
    use std::os::unix::fs::MetadataExt;
    let device = meta.rdev() as libc::dev_t;
    (libc::major(device) as u32, libc::minor(device) as u32)
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
    if std::str::from_utf8(&uname).is_err()
        || std::str::from_utf8(&gname).is_err()
        || std::str::from_utf8(&member.name).is_err()
        || member
            .link
            .as_ref()
            .is_some_and(|link| std::str::from_utf8(link).is_err())
    {
        records.extend(pax_record("hdrcharset", b"BINARY"));
    }
    for (key, value) in [("uname", &uname), ("gname", &gname)] {
        if !value.is_ascii() || value.len() > 32 {
            records.extend(pax_record(key, value));
        }
    }
    if !member.name.is_ascii() || member.name.len() > 100 {
        records.extend(pax_record("path", &member.name));
    }
    if let Some(link) = &member.link
        && (!link.is_ascii() || link.len() > 100)
    {
        records.extend(pax_record("linkpath", link));
    }
    records.extend(pax_record("mtime", float_text(mtime)?.as_bytes()));
    if !records.is_empty() {
        let mut pax = Header::new_ustar();
        ascii_field(&mut pax.as_old_mut().name, b"././@PaxHeader");
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
    ascii_field(&mut ustar.uname, &uname);
    ascii_field(&mut ustar.gname, &gname);
    header.set_size(size);
    #[cfg(unix)]
    if matches!(member.kind, EntryType::Char | EntryType::Block) {
        let (major, minor) = device_numbers(member.meta);
        header.set_device_major(major)?;
        header.set_device_minor(minor)?;
    }
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
fn add<W: io::Write>(
    tar: &mut Builder<W>,
    path: &Path,
    arcname: &[u8],
    inodes: &mut HashMap<(u64, u64), Vec<u8>>,
) -> io::Result<()> {
    let meta = metadata(path)?;
    let mut name = arcname.to_vec();
    let mut link = None;
    let kind;
    if is_link(path, &meta).ok_or_else(|| io::Error::other("reparse metadata unavailable"))? {
        kind = EntryType::Symlink;
        link = Some(name_bytes(read_link(path)?.as_os_str()));
    } else if is_directory(&meta) {
        kind = EntryType::Directory;
        name.push(b'/');
    } else if is_regular(&meta) {
        let (inode, links) = identity(path, &meta)?;
        if links > 1
            && let Some(previous) = inodes.get(&inode)
        {
            kind = EntryType::Link;
            link = Some(previous.clone());
        } else {
            kind = EntryType::Regular;
            if inode.1 != 0 {
                inodes.insert(inode, name.clone());
            }
        }
    } else {
        #[cfg(unix)]
        {
            use std::os::unix::fs::FileTypeExt;
            let file_type = meta.file_type();
            kind = if file_type.is_fifo() {
                EntryType::Fifo
            } else if file_type.is_char_device() {
                EntryType::Char
            } else if file_type.is_block_device() {
                EntryType::Block
            } else {
                return Ok(());
            }; // Python omits sockets and unrecognized kinds.
        }
        #[cfg(windows)]
        {
            return Ok(());
        }
    }
    append(
        tar,
        Member {
            name,
            kind,
            meta: &meta,
            path,
            link,
        },
    )?;
    if kind == EntryType::Directory {
        let names = children(path, false)?;
        // os.listdir names sorted as str: code-point order on every platform.
        for (name, child) in names {
            let mut child_name = arcname.to_vec();
            child_name.push(b'/');
            child_name.extend(name_bytes(&name));
            add(tar, &child, &child_name, inodes)?;
        }
    }
    Ok(())
}

pub(super) fn write(data_dir: &Path, bdir: &Path, ts: &str) -> io::Result<PathBuf> {
    let target = super::args::join(bdir, &format!("{}{ts}.tar.gz", super::STATE_PREFIX));
    let partial = super::args::join(bdir, &format!("{}{ts}.tar.gz.part", super::STATE_PREFIX));
    let written = (|| -> io::Result<()> {
        let gz = GzEncoder::new(fs::File::create(&partial)?, Compression::best());
        let mut tar = Builder::new(gz);
        let children = top_children(data_dir)
            .ok_or_else(|| io::Error::other("data dir could not be listed"))?;
        let mut inodes = HashMap::new();
        for (name, path) in children {
            if excluded(&name, &path, Some(bdir)) {
                continue;
            }
            add(&mut tar, &path, &name_bytes(&name), &mut inodes)?;
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
    fn admission_rejects_a_directory_already_on_the_active_walk() {
        let directory = std::env::temp_dir().join(format!("backup-cycle-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&directory).unwrap();
        let root = fs::canonicalize(&directory).unwrap();
        let meta = metadata(&root).unwrap();
        let mut active = HashSet::from([root.clone()]);
        let result = supported(
            &root,
            &meta,
            &root,
            &root.with_extension("outside"),
            &mut active,
            #[cfg(windows)]
            &HashSet::new(),
        );
        fs::remove_dir(directory).unwrap();
        assert_eq!(result, None);
    }

    #[test]
    fn directory_link_target_must_fit_python_inference_buffer() {
        assert!(directory_link_target_supported(true, 259));
        assert!(!directory_link_target_supported(true, 260));
        assert!(!directory_link_target_supported(false, 10));
    }

    #[cfg(windows)]
    #[test]
    fn windows_names_must_encode_and_decode_to_the_same_codepoints() {
        use std::os::windows::ffi::OsStringExt;
        for (wide, expected) in [
            (&[0xdcc2, 0xdca9][..], false),
            (&[0xdcff][..], true),
            (&[0xd800][..], false),
            (&[0xd83d, 0xde80][..], true),
        ] {
            assert_eq!(encodable(&OsString::from_wide(wide)), expected, "{wide:?}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn filesystem_sort_keeps_surrogates_before_private_use_codepoints() {
        use std::os::unix::ffi::OsStrExt;
        let name = OsStr::from_bytes(b"Case-\xff");
        assert_eq!(
            name_sort_key(name, true),
            vec![99, 97, 115, 101, 45, 0xdcff]
        );
        assert!(name_sort_key(name, true) < name_sort_key(OsStr::new("case-\u{e000}"), true));
    }

    #[test]
    fn reparse_substitute_names_match_python_without_user_path_conversion() {
        for (target, expected) in [
            (r"\??\C:\outside", r"\\?\C:\outside"),
            (r"\??\UNC\server\share", r"\\?\UNC\server\share"),
            ("absent", "absent"),
        ] {
            let wide = target.encode_utf16().collect::<Vec<_>>();
            let mut data = vec![0u8; 20];
            data[..4].copy_from_slice(&0xa000000cu32.to_le_bytes());
            data[10..12].copy_from_slice(&((wide.len() * 2) as u16).to_le_bytes());
            for character in wide {
                data.extend(character.to_le_bytes());
            }
            assert_eq!(
                String::from_utf16(&substitute_name(&data).unwrap()).unwrap(),
                expected
            );
            data.pop();
            assert!(substitute_name(&data).is_err());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn character_device_header_uses_stat_kind_and_device_numbers() {
        use std::os::unix::fs::MetadataExt;
        let source = Path::new("/dev/null");
        let metadata = fs::symlink_metadata(source).unwrap();
        let mut tar = Builder::new(Vec::new());
        add(&mut tar, source, b"character", &mut HashMap::new()).unwrap();
        let bytes = tar.into_inner().unwrap();
        let mut archive = tar::Archive::new(bytes.as_slice());
        let entry = archive.entries().unwrap().next().unwrap().unwrap();
        assert_eq!(entry.header().entry_type(), EntryType::Char);
        assert_eq!(entry.size(), 0);
        assert_eq!(
            entry.header().device_major().unwrap(),
            Some(libc::major(metadata.rdev()))
        );
        assert_eq!(
            entry.header().device_minor().unwrap(),
            Some(libc::minor(metadata.rdev()))
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
