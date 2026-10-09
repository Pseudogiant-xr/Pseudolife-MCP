//! `_archive_state`: a tar.gz of the data dir minus the bank and the backups.
//!
//! Members carry what Python's `tarfile` records and reads back (name, type,
//! mode, uid, gid, size, float mtime, link target, content). Container bytes
//! (header encoding, PAX layout, gzip header, padding) are not contract.
use flate2::{Compression, write::GzEncoder};
use std::{
    fs::{self, Metadata},
    io,
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

/// Admit the data dir's tree before any effect.
pub(super) fn admit(data_dir: &Path, bdir: &Path) -> Option<()> {
    let bdir = bdir.exists().then_some(bdir);
    for (name, path) in top_children(data_dir)? {
        if excluded(&name, &path, bdir) {
            continue;
        }
        supported(&path, &fs::symlink_metadata(&path).ok()?)?;
    }
    Some(())
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
    let executable = path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| {
            ["exe", "bat", "cmd", "com"]
                .iter()
                .any(|x| ext.eq_ignore_ascii_case(x))
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

/// Python `str(float)`: shortest round-trip digits, `.0` on integral values.
fn float_text(value: f64) -> String {
    let text = format!("{value}");
    if text.contains(['.', 'e', 'N', 'i']) {
        text
    } else {
        format!("{text}.0")
    }
}

fn pax_record(key: &str, value: &str) -> Vec<u8> {
    let payload = format!(" {key}={value}\n");
    let mut length = payload.len() + 1;
    while length != payload.len() + length.to_string().len() {
        length = payload.len() + length.to_string().len();
    }
    format!("{length}{payload}").into_bytes()
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
    let mut records = Vec::new();
    if !member.name.is_ascii() || member.name.len() > 100 {
        records.extend(pax_record("path", &member.name));
    }
    if let Some(link) = &member.link
        && (!link.is_ascii() || link.len() > 100)
    {
        records.extend(pax_record("linkpath", link));
    }
    records.extend(pax_record("mtime", &float_text(mtime)));
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
    header.set_size(size);
    header.set_mtime(mtime.round_ties_even().max(0.0) as u64);
    header.set_cksum();
    if member.kind == EntryType::Regular {
        tar.append(&header, fs::File::open(member.path)?)
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
    let target = bdir.join(format!("{}{ts}.tar.gz", super::STATE_PREFIX));
    let partial = bdir.join(format!("{}{ts}.tar.gz.part", super::STATE_PREFIX));
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
