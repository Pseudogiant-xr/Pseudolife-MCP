//! Which links a private path may pass through: the one rule the token
//! file, the board's state and every other private file of this client use
//! (`credentials::reject_ancestor_redirects` wraps it).
//!
//! What it protects: only this user, root (on Windows SYSTEM or
//! Administrators) may decide where a private path leads, so a write cannot
//! be steered into a place someone else chose and a read cannot be served
//! someone else's file. A link on the path is therefore followed only when
//! nobody else could have made it or can change it:
//!
//! - POSIX: the link is owned by this user or root, and the folder holding
//!   it is owned by this user or root and is not group- or world-writable
//!   unless it is sticky (in a sticky folder such as `/tmp` only an entry's
//!   owner, the folder's owner and root can remove or replace it).
//! - Windows: only symlinks and junctions are links. The link and the folder
//!   holding it are owned by this user, SYSTEM or Administrators; no other
//!   account may delete, rewrite or re-permission the link, and none may
//!   delete entries in its folder or change that folder's access list. A
//!   cloud-files placeholder (OneDrive Files On-Demand) is a reparse point
//!   that redirects nothing and counts as a plain folder; any other reparse
//!   point is refused.
//!
//! An accepted link's target is checked the same way, itself and every
//! folder above it, so a trusted link cannot lead through an untrusted link.
//! The plain folders it leads to get no more checks than a path configured
//! directly to them would: a link into a folder others may write is as
//! exposed as naming that folder in a setting.
//! Plain folders get no further checks than they always had: what was
//! accepted before is still accepted. The leaf itself (the file, or the
//! folder `check_path` is given) is never a link the caller follows: callers
//! keep their own leaf checks (`O_NOFOLLOW`, `FILE_FLAG_OPEN_REPARSE_POINT`
//! and the checks on the opened handle).
use std::{
    collections::HashSet,
    fmt, io,
    path::{Path, PathBuf},
    sync::{LazyLock, Mutex},
};

/// Links followed for one check before it is refused as a loop, as the
/// POSIX limit on symlinks in one lookup (`SYMLOOP_MAX`, 40 on Linux).
pub const MAX_LINKS: usize = 40;

/// Why a private path is refused: the element on it and the cause.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub element: PathBuf,
    /// Boxed: the refusal travels as an `Err` through every caller.
    pub cause: Box<Cause>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Cause {
    /// The link is owned by another account.
    LinkOwner { owner: String, target: PathBuf },
    /// Another account may delete, rewrite or re-permission the link (Windows).
    LinkWriter { writer: String, target: PathBuf },
    /// The folder holding the link is owned by another account.
    HolderOwner {
        holder: PathBuf,
        owner: String,
        target: PathBuf,
    },
    /// Others may remove or replace entries in the folder holding the link.
    /// `owned`: that folder is this user's own, so tightening it is theirs to
    /// do.
    HolderWritable {
        holder: PathBuf,
        writer: Writer,
        owned: bool,
        target: PathBuf,
    },
    /// More than [`MAX_LINKS`] links, usually a loop.
    TooManyLinks,
    /// A Windows reparse point that is neither a link nor a cloud placeholder.
    NotALink { tag: u32 },
    /// A link whose target cannot be checked (a Windows device path, or
    /// reparse data that is malformed or changed while it was read).
    UncheckableTarget { detail: String },
    /// The element could not be inspected.
    Uninspectable { detail: String },
}

/// The fix for a link this user should not keep: it may be impossible to
/// repair in place (another account's link or folder), and following it
/// literally must not leave the client looking at an empty folder.
const REPLACE: &str = "replace the link with a folder you own and move what it led to there";

/// `path` quoted for a POSIX shell: in single quotes, each `'` written `'\''`.
fn shell_quoted(path: &Path) -> String {
    format!("'{}'", path.display().to_string().replace('\'', r"'\''"))
}

/// Who besides this user may change entries in a link's folder.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Writer {
    Group,
    Others,
    GroupAndOthers,
    Account(String),
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let element = self.element.display();
        let lead =
            "pseudolife-mcp keeps private files only behind links no other account can change";
        match &*self.cause {
            Cause::LinkOwner { owner, .. } => write!(
                f,
                "{lead}, and {element} is a link owned by {owner}; fix: {REPLACE}."
            ),
            Cause::LinkWriter { writer, .. } => write!(
                f,
                "{lead}, and {writer} may delete or rewrite the link {element}; fix: {REPLACE}."
            ),
            Cause::HolderOwner { holder, owner, .. } => write!(
                f,
                "{lead}, and the link {element} sits in {}, which {owner} owns and so can replace it; fix: {REPLACE}.",
                holder.display()
            ),
            Cause::HolderWritable {
                holder,
                writer: Writer::Account(account),
                ..
            } => write!(
                f,
                "{lead}, and {account} may delete or replace entries in {}, which holds the link {element}; fix: {REPLACE}.",
                holder.display()
            ),
            Cause::HolderWritable {
                holder,
                writer,
                owned,
                ..
            } => {
                let (who, bits) = match writer {
                    Writer::Group => ("the folder's group", "g-w"),
                    Writer::Others => ("other users", "o-w"),
                    _ => ("the folder's group and other users", "go-w"),
                };
                // Tightening a folder is the fix only for one this user owns:
                // never a system folder such as a root-owned /usr/local.
                let fix = if *owned {
                    format!("chmod {bits} {}", shell_quoted(holder))
                } else {
                    REPLACE.to_owned()
                };
                write!(
                    f,
                    "{lead}, and {who} can replace the link {element} because they may write {}; fix: {fix}.",
                    holder.display()
                )
            }
            Cause::TooManyLinks => write!(
                f,
                "{lead}, and {element} leads through more than {MAX_LINKS} links (a loop?); fix: point it at the folder itself."
            ),
            Cause::NotALink { tag } => write!(
                f,
                "{lead}, and {element} is a Windows reparse point of tag {tag:#010x}, which is neither a symlink, a junction nor a cloud-files placeholder; fix: keep this client's files in a plain folder."
            ),
            Cause::UncheckableTarget { detail } => write!(
                f,
                "{lead}, and where the link {element} leads cannot be checked ({detail}); fix: {REPLACE}."
            ),
            Cause::Uninspectable { detail } => write!(
                f,
                "pseudolife-mcp cannot inspect {element} on the way to a private file ({detail}); fix: let this account read that folder's attributes and permissions, then retry."
            ),
        }
    }
}
impl std::error::Error for Refusal {}

/// The POSIX rule for one link: `me` is this process's effective user;
/// `link_owner` the link's owner (`lstat`); `holder_owner` and `holder_mode`
/// the folder holding it. `None` trusts the link.
#[cfg_attr(windows, allow(dead_code))]
pub(crate) fn posix_link_cause(
    me: u32,
    link_owner: u32,
    holder_owner: u32,
    holder_mode: u32,
) -> Option<PosixDistrust> {
    let trusted = |owner: u32| owner == me || owner == 0;
    if !trusted(link_owner) {
        return Some(PosixDistrust::LinkOwner(link_owner));
    }
    if !trusted(holder_owner) {
        return Some(PosixDistrust::HolderOwner(holder_owner));
    }
    // A sticky folder lets only an entry's owner, its own owner and root
    // remove or rename that entry, and both owners are trusted above.
    if holder_mode & 0o1000 != 0 {
        return None;
    }
    match (holder_mode & 0o020 != 0, holder_mode & 0o002 != 0) {
        (false, false) => None,
        (true, false) => Some(PosixDistrust::HolderWritable(Writer::Group)),
        (false, true) => Some(PosixDistrust::HolderWritable(Writer::Others)),
        (true, true) => Some(PosixDistrust::HolderWritable(Writer::GroupAndOthers)),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
#[cfg_attr(windows, allow(dead_code))]
pub(crate) enum PosixDistrust {
    LinkOwner(u32),
    HolderOwner(u32),
    HolderWritable(Writer),
}

/// What a Windows reparse tag is to this rule.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ReparseKind {
    /// A symlink or a junction (mount point): a name surrogate that sends
    /// the path elsewhere.
    Link,
    /// A cloud-files placeholder (`IO_REPARSE_TAG_CLOUD` to `_CLOUD_F`): the
    /// folder stays where it is; a sync engine supplies its contents.
    Placeholder,
    Other,
}

/// `IO_REPARSE_TAG_MOUNT_POINT` and `IO_REPARSE_TAG_SYMLINK` (winnt.h).
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) const TAG_MOUNT_POINT: u32 = 0xA000_0003;
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) const TAG_SYMLINK: u32 = 0xA000_000C;
/// `IO_REPARSE_TAG_CLOUD`; `_CLOUD_1` to `_CLOUD_F` differ only in the bits
/// of `IO_REPARSE_TAG_CLOUD_MASK` (0x0000F000), per [MS-FSCC] 2.1.2.1.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) const TAG_CLOUD: u32 = 0x9000_001A;
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) const TAG_CLOUD_MASK: u32 = 0x0000_F000;

/// The exact signals, nothing broader: the two link tags, and the
/// sixteen cloud-files tags.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn reparse_kind(tag: u32) -> ReparseKind {
    if tag == TAG_MOUNT_POINT || tag == TAG_SYMLINK {
        ReparseKind::Link
    } else if tag & !TAG_CLOUD_MASK == TAG_CLOUD {
        ReparseKind::Placeholder
    } else {
        ReparseKind::Other
    }
}

/// Check every folder above `path` (not `path` itself).
pub fn check_ancestors(path: &Path) -> Result<(), Refusal> {
    match path.parent() {
        Some(parent) => Walk::default().walk(parent),
        None => Ok(()),
    }
}

/// Check `path` itself and every folder above it: for a private folder the
/// caller uses as a folder, where a link in its place is followed too.
pub fn check_path(path: &Path) -> Result<(), Refusal> {
    Walk::default().walk(path)
}

/// [`check_ancestors`] for a client's own config file (the maintainer's
/// ruling on #2032): on Linux a link in a folder only its group may also
/// write is trusted when that group is the user's own private group (as
/// umask 002 leaves `~/.config` on user-private-group systems), because no
/// other account is in it. Elsewhere it is the plain rule.
pub fn check_client_config_ancestors(path: &Path) -> Result<(), Refusal> {
    #[cfg(unix)]
    {
        check_ancestors_with(path, Accounts::system())
    }
    #[cfg(not(unix))]
    {
        check_ancestors(path)
    }
}

#[cfg(unix)]
pub(crate) fn check_ancestors_with(path: &Path, accounts: Accounts) -> Result<(), Refusal> {
    match path.parent() {
        Some(parent) => Walk {
            accounts: Some(accounts),
            ..Walk::default()
        }
        .walk(parent),
        None => Ok(()),
    }
}

/// The local account files, read to tell a user's private group from a
/// shared one. An account they do not list (one from LDAP, say) is never
/// taken for a private group, and nor is any group while the system may
/// also take accounts or groups from a source those files do not show
/// (`nsswitch.conf`, systemd's userdb services), so its folders keep the
/// plain rule.
/// `/etc/gshadow` is readable only by root and is not consulted.
#[cfg(unix)]
#[derive(Clone, Debug, Default)]
pub(crate) struct Accounts {
    passwd: String,
    group: String,
    /// `passwd`, `group` and `initgroups` come only from these files.
    files_only: bool,
}

/// Whether `nsswitch.conf` takes `passwd`, `group` and `initgroups` only from
/// the local files. `files` counts, and `systemd` only when
/// `systemd_local` says its userdb serves nothing beyond them (see
/// [`Accounts::under`]); anything else, `compat`'s NIS `+`/`-` lines
/// included, does not. Both `passwd` and `group` must be named, each line
/// with `files` in it (one with no source is glibc's default, unread);
/// `initgroups` falls back to `group`. As glibc does, a database name ends
/// at whitespace or `:`, case aside.
#[cfg(unix)]
pub(crate) fn nss_files_only(nsswitch: &str, systemd_local: bool) -> bool {
    let (mut passwd, mut group) = (false, false);
    let local = nsswitch.lines().all(|line| {
        let line = line.split('#').next().unwrap_or("").trim_start();
        let end = line
            .find(|c: char| c.is_whitespace() || c == ':')
            .unwrap_or(line.len());
        let (database, sources) = line.split_at(end);
        let sources = sources.trim_start_matches(|c: char| c.is_whitespace() || c == ':');
        match database.to_ascii_lowercase().as_str() {
            "passwd" => passwd = true,
            "group" => group = true,
            // `initgroups` can add supplementary groups on its own.
            "initgroups" => {}
            _ => return true,
        }
        // A `[...]` action group is skipped only when its brackets stand
        // alone; a service name glued to one (`[...]sss`) fails.
        let mut in_action = false;
        let mut files = false;
        sources.split_whitespace().all(|word| {
            if in_action || word.starts_with('[') {
                match word.find(']') {
                    Some(end) if end + 1 == word.len() => in_action = false,
                    Some(_) => return false,
                    None => in_action = true,
                }
                return true;
            }
            files |= word == "files";
            word == "files" || (systemd_local && word == "systemd")
        }) && !in_action
            && files
    });
    local && passwd && group
}

/// The colon-separated records of an account file, or `None` when any line
/// that is not blank or a comment does not have `width` fields or is a
/// compat `+`/`-` reference to another source. (glibc skips leading
/// whitespace, which this reader keeps: such a name never matches the
/// user's, so the line can only make a group look less private.)
#[cfg(unix)]
fn records(text: &str, width: usize) -> Option<Vec<Vec<&str>>> {
    let mut rows = Vec::new();
    for line in text.lines() {
        let content = line.trim_start();
        if content.is_empty() || content.starts_with('#') {
            continue;
        }
        if line.starts_with(['+', '-']) {
            return None;
        }
        let fields: Vec<&str> = line.split(':').collect();
        if fields.len() != width {
            return None;
        }
        rows.push(fields);
    }
    Some(rows)
}

/// A numeric id field. glibc's `strtoul` also reads ` 1000` or `-1`, which
/// this parse does not, so the caller treats `None` as not private, never
/// as a line to skip.
#[cfg(unix)]
fn numeric_id(field: &str) -> Option<u32> {
    field.parse().ok()
}

/// The folders systemd's userdb reads drop-in records from (`*.user`,
/// `*.group`, `*.membership`), relative to the root.
#[cfg(unix)]
const USERDB_DIRS: [&str; 5] = [
    "etc/userdb",
    "run/userdb",
    "run/host/userdb",
    "usr/lib/userdb",
    "usr/local/lib/userdb",
];

/// The varlink services in `/run/systemd/userdb` that add no account,
/// group or membership the local files lack, while no drop-in record exists:
/// PID 1's `DynamicUser` (ids from [`SYSTEMD_RANGES`] only, no memberships),
/// and systemd-userdbd's `Multiplexer`, `NameServiceSwitch` (which
/// nss-systemd skips, and which serves glibc's own NSS records) and `DropIn`
/// (the drop-in records). Any other service (`io.systemd.Home`,
/// `io.systemd.Machine`, `io.systemd.NamespaceResource`, one unknown) may.
#[cfg(unix)]
const SYSTEMD_LOCAL_SERVICES: [&str; 4] = [
    "io.systemd.DynamicUser",
    "io.systemd.Multiplexer",
    "io.systemd.NameServiceSwitch",
    "io.systemd.DropIn",
];

/// Ids systemd hands out or synthesizes itself, never a user's private
/// group here (systemd's `docs/UIDS-GIDS.md`; real user-private-group ids
/// sit below 60000): `DynamicUser=` (61184-65519); `nobody` (65534, which
/// nss-systemd synthesizes as a primary group when `/etc/passwd` lacks it)
/// and 65535; containers (0x80000-0x6FFFFFFF: `--private-users=pick` checks
/// only a range's base); systemd-nsresourced (0x70000000-0x7FFDFFFF); and
/// the foreign range (0x7FFE0000-0x7FFEFFFF).
#[cfg(unix)]
const SYSTEMD_RANGES: [std::ops::RangeInclusive<u32>; 5] = [
    61184..=65519,
    65534..=65535,
    0x0008_0000..=0x6FFF_FFFF,
    0x7000_0000..=0x7FFD_FFFF,
    0x7FFE_0000..=0x7FFE_FFFF,
];

/// Where glibc may read `nsswitch.conf`, relative to the root: openSUSE's
/// glibc falls back to `/usr/etc` when `/etc` has none.
#[cfg(unix)]
const NSSWITCH_FILES: [&str; 2] = ["usr/etc/nsswitch.conf", "etc/nsswitch.conf"];

#[cfg(unix)]
impl Accounts {
    pub(crate) fn system() -> Self {
        Self::under(Path::new("/"))
    }

    /// The account files under `root` (`/` but in tests). Files-only needs
    /// at least one `nsswitch.conf`, every one that exists files-only (a
    /// missing one is glibc's built-in default, not read here, so it fails
    /// closed), and no userdb drop-in record at all.
    ///
    /// `systemd` there counts as the files only while `/run/systemd/userdb`
    /// holds nothing but [`SYSTEMD_LOCAL_SERVICES`]; an unreadable folder
    /// does not count. nss-systemd finds its services by listing that folder
    /// and treats a missing one as no service at all, which leaves only the
    /// drop-ins (refused here) and the `root`, `nobody` and foreign groups it
    /// synthesizes, which have no members.
    pub(crate) fn under(root: &Path) -> Self {
        let userdb = USERDB_DIRS
            .iter()
            .any(|dir| match std::fs::read_dir(root.join(dir)) {
                Ok(mut entries) => entries.next().is_some(),
                Err(error) => error.kind() != io::ErrorKind::NotFound,
            });
        // A drop-in record already makes it not files-only (below).
        let systemd_local = match std::fs::read_dir(root.join("run/systemd/userdb")) {
            Ok(mut entries) => entries.all(|entry| {
                entry.is_ok_and(|entry| {
                    SYSTEMD_LOCAL_SERVICES
                        .iter()
                        .any(|service| entry.file_name() == *service)
                })
            }),
            // No folder: nss-systemd has no service to ask.
            Err(error) => error.kind() == io::ErrorKind::NotFound,
        };
        let mut found = false;
        let nsswitch_local =
            NSSWITCH_FILES
                .iter()
                .all(|file| match std::fs::read_to_string(root.join(file)) {
                    Ok(text) => {
                        found = true;
                        nss_files_only(&text, systemd_local)
                    }
                    Err(error) => error.kind() == io::ErrorKind::NotFound,
                });
        Self {
            passwd: std::fs::read_to_string(root.join("etc/passwd")).unwrap_or_default(),
            group: std::fs::read_to_string(root.join("etc/group")).unwrap_or_default(),
            files_only: found && nsswitch_local && !userdb,
        }
    }

    #[cfg(test)]
    pub(crate) fn from_text(passwd: &str, group: &str) -> Self {
        Self::from_files(passwd, group, "passwd: files\ngroup: files\n")
    }

    #[cfg(test)]
    pub(crate) fn from_files(passwd: &str, group: &str, nsswitch: &str) -> Self {
        Self {
            passwd: passwd.to_owned(),
            group: group.to_owned(),
            files_only: nss_files_only(nsswitch, false),
        }
    }

    /// `gid` is `uid`'s private group: its primary group, named as the
    /// user is, with no other member, and no other account's primary group.
    /// Any record line that does not read cleanly makes it not private.
    pub(crate) fn private_group(&self, uid: u32, gid: u32) -> bool {
        if !self.files_only || SYSTEMD_RANGES.iter().any(|range| range.contains(&gid)) {
            return false;
        }
        let (Some(passwd), Some(group)) = (records(&self.passwd, 7), records(&self.group, 4))
        else {
            return false;
        };
        let mut users = Vec::new();
        for fields in &passwd {
            let (Some(id), Some(primary)) = (numeric_id(fields[2]), numeric_id(fields[3])) else {
                return false;
            };
            users.push((fields[0], id, primary));
        }
        let Some(&(name, _, primary)) = users.iter().find(|(_, id, _)| *id == uid) else {
            return false;
        };
        if primary != gid
            || users
                .iter()
                .any(|(other, id, group)| *group == gid && (*id != uid || *other != name))
        {
            return false;
        }
        // Every entry for the gid, not only the first: a second one with
        // another name or members makes it shared.
        let mut entries = Vec::new();
        for fields in &group {
            let Some(id) = numeric_id(fields[2]) else {
                return false;
            };
            if id == gid {
                entries.push((fields[0], fields[1], fields[3]));
            }
        }
        // A group password lets anyone who knows it join with newgrp: only
        // a locked or shadowed field (`x`, `!`, `*`, empty) counts. What
        // `/etc/gshadow` holds behind `x` (readable only by root) only root
        // can set, and root is not another account this rule guards against.
        !entries.is_empty()
            && entries.iter().all(|(group, password, members)| {
                matches!(*password, "x" | "!" | "*" | "")
                    && *group == name
                    && members
                        .split(',')
                        .map(str::trim)
                        .all(|member| member.is_empty() || member == name)
            })
    }
}

/// The folder is on a local filesystem whose gids are this host's own: a
/// network or FUSE filesystem may map them differently, so it fails.
#[cfg(target_os = "linux")]
pub(crate) fn on_local_filesystem(path: &Path) -> bool {
    // ext2/3/4, xfs, btrfs, tmpfs, f2fs, zfs, bcachefs, overlayfs.
    const LOCAL: [i64; 8] = [
        0xEF53,
        0x5846_5342,
        0x9123_683E,
        0x0102_1994,
        0xF2F5_2010,
        0x2FC1_2FC1,
        0xCA45_1A4E,
        0x794C_7630,
    ];
    // `f_type`'s width differs between targets.
    #[allow(clippy::unnecessary_cast)]
    rustix::fs::statfs(path).is_ok_and(|fs| LOCAL.contains(&(fs.f_type as i64)))
}

/// No ACL on `path` that could let another account write: with a POSIX
/// access ACL the mode's group bits are only its mask.
#[cfg(target_os = "linux")]
fn without_access_acl(path: &Path) -> bool {
    // A zero-length read returns the attribute's size without its value.
    let mut empty: [u8; 0] = [];
    acls_allow(
        rustix::fs::getxattr(path, "system.posix_acl_access", &mut empty[..]),
        rustix::fs::getxattr(path, "system.nfs4_acl", &mut empty[..]),
    )
}

/// What the two ACL reads must say: only a filesystem that supports POSIX
/// ACLs and reports none passes (one that does not support them is unknown,
/// so it fails), and an NFSv4 ACL fails too; any other error fails.
#[cfg(target_os = "linux")]
pub(crate) fn acls_allow(
    posix: rustix::io::Result<usize>,
    nfs4: rustix::io::Result<usize>,
) -> bool {
    use rustix::io::Errno;
    matches!(posix, Ok(0) | Err(Errno::NODATA))
        && matches!(nfs4, Ok(0) | Err(Errno::NODATA | Errno::NOTSUP))
}

/// How many distinct links [`check_ancestors`] follows for `path`: for the
/// tests that each link costs one against [`MAX_LINKS`], however it is
/// spelled when the walk meets it.
#[cfg(test)]
pub(crate) fn links_followed(path: &Path) -> Result<usize, Refusal> {
    let mut walk = Walk::default();
    if let Some(parent) = path.parent() {
        walk.walk(parent)?;
    }
    Ok(walk.links)
}

/// The refusal line for the folders above `path`, when there is one.
pub fn explain(path: &Path) -> Option<String> {
    check_ancestors(path)
        .err()
        .map(|refusal| refusal.to_string())
}

/// Print a refusal once per element for this process, on stderr.
pub fn report_once(refusal: &Refusal) {
    static REPORTED: LazyLock<Mutex<HashSet<PathBuf>>> = LazyLock::new(Default::default);
    if REPORTED
        .lock()
        .map(|mut seen| seen.insert(refusal.element.clone()))
        .unwrap_or(false)
    {
        crate::stderrln!("{refusal}");
    }
}

/// What the plugin hooks print on stdout when the folders above a private
/// file pass the rule: they trust nothing else (an older client, or one
/// that is not native, answers an unknown command some other way).
pub const HOOK_TRUSTED: &str = "pseudolife-private-path: trusted";

/// What precedes the refusal line [`hook_command`] prints for the hooks.
pub const HOOK_REFUSED: &str = "pseudolife-private-path: refused: ";

/// `pseudolife-mcp private-path-check <absolute file path>`, hidden: the
/// plugin hooks ask it whether the folders above a private file they read
/// pass this rule, so the rule has one implementation. It reads nothing but
/// the path's metadata and changes nothing. Exit 0 with [`HOOK_TRUSTED`] on
/// stdout when they pass; 1 with [`HOOK_REFUSED`] and the refusal on stdout
/// (the hooks relay it); 2 on bad usage.
pub fn hook_command(arguments: Vec<std::ffi::OsString>) -> u8 {
    let [path] = arguments.as_slice() else {
        crate::stderrln!("usage: pseudolife-mcp private-path-check <absolute file path>");
        return 2;
    };
    let path = Path::new(path);
    if !path.is_absolute() {
        crate::stderrln!("usage: pseudolife-mcp private-path-check <absolute file path>");
        return 2;
    }
    match check_ancestors(path) {
        Ok(()) => {
            println!("{HOOK_TRUSTED}");
            0
        }
        Err(refusal) => {
            println!("{HOOK_REFUSED}{refusal}");
            1
        }
    }
}

/// One check's state: the links already accepted (walked once each, so
/// nested links cost what they are, not what their prefixes repeat), the
/// links being resolved (one met again is a loop), and how many links the
/// check has followed. A link is remembered by its filesystem identity
/// together with the destination it resolves to here: a hit only spares a
/// second walk of that very destination, and the link's own checks (owner,
/// holder, access list) run before the lookup every time. So an identity two
/// entries share (a filesystem without stable file IDs, a hard-linked
/// symlink) can never excuse walking a different destination, and a zero
/// identity is keyed like any other.
#[derive(Default)]
struct Walk {
    accepted: HashSet<LinkKey>,
    resolving: HashSet<LinkKey>,
    links: usize,
    /// Set for a client config: a user's private group is no other account.
    #[cfg(unix)]
    accounts: Option<Accounts>,
}

type LinkKey = ((u64, u64), PathBuf);

impl Walk {
    fn walk(&mut self, path: &Path) -> Result<(), Refusal> {
        // Each prefix is rebuilt from the path's components, never taken from
        // `Path::ancestors`: a prefix written `inner/` or `inner/.` would make
        // the look below follow `inner` instead of inspecting it.
        let mut prefix = PathBuf::new();
        for component in path.components() {
            prefix.push(component);
            if matches!(component, std::path::Component::Prefix(_)) {
                // A bare drive (`C:`) names a current directory, not an entry.
                continue;
            }
            self.element(&prefix)?;
        }
        Ok(())
    }

    fn element(&mut self, prefix: &Path) -> Result<(), Refusal> {
        let refuse = |cause| {
            Err(Refusal {
                element: prefix.to_path_buf(),
                cause: Box::new(cause),
            })
        };
        #[cfg(unix)]
        let inspected = inspect(prefix, self.accounts.as_ref());
        #[cfg(not(unix))]
        let inspected = inspect(prefix);
        match inspected {
            Ok(Entry::Plain) => Ok(()),
            Ok(Entry::Link {
                target,
                distrust,
                identity,
            }) => {
                // Every link is inspected each time it is met; one already
                // accepted in this check (the same object, however its path
                // was spelled or cased) is not walked or counted again.
                if let Some(cause) = distrust {
                    return refuse(cause);
                }
                // Where the link leads: absolute, or relative to the folder
                // holding it. The OS resolves `..` in what follows as it does
                // in the lookup itself, so the walk checks the very folders
                // the open will cross.
                let resolved = match prefix.parent() {
                    Some(holder) => holder.join(&target),
                    None => target.clone(),
                };
                // Only after the link's own checks above: a hit must never
                // stand in for them.
                let key = (identity, resolved.clone());
                if self.accepted.contains(&key) {
                    return Ok(());
                }
                self.links += 1;
                if self.links > MAX_LINKS || !self.resolving.insert(key.clone()) {
                    return refuse(Cause::TooManyLinks);
                }
                self.walk(&resolved)?;
                self.resolving.remove(&key);
                self.accepted.insert(key);
                Ok(())
            }
            Ok(Entry::Other(tag)) => refuse(Cause::NotALink { tag }),
            Err(error) => refuse(Cause::Uninspectable {
                detail: error.to_string(),
            }),
        }
    }
}

enum Entry {
    /// A plain folder or file, a missing one, or a cloud placeholder.
    Plain,
    /// A link, where it leads, why it is not trusted (if it is not), and
    /// the link entry's own filesystem identity.
    Link {
        target: PathBuf,
        distrust: Option<Cause>,
        identity: (u64, u64),
    },
    /// A Windows reparse point of another kind.
    #[cfg_attr(not(windows), allow(dead_code))]
    Other(u32),
}

#[cfg(unix)]
fn inspect(path: &Path, accounts: Option<&Accounts>) -> io::Result<Entry> {
    use std::os::unix::fs::MetadataExt;
    let meta = match std::fs::symlink_metadata(path) {
        Ok(meta) => meta,
        // Not made yet: what is created there is checked when it is.
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Entry::Plain),
        Err(error) => return Err(error),
    };
    if !meta.file_type().is_symlink() {
        return Ok(Entry::Plain);
    }
    let target = std::fs::read_link(path)?;
    let holder_path = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    // Followed: the folders above it were checked first.
    let holder = std::fs::metadata(holder_path)?;
    let me = rustix::process::geteuid().as_raw();
    let shown = target.clone();
    // Linux only: there the ACL can be read, and elsewhere (macOS) the
    // primary group is the shared `staff` anyway.
    let private_group = |gid: u32| {
        cfg!(target_os = "linux")
            && accounts.is_some_and(|accounts| accounts.private_group(me, gid))
            && {
                #[cfg(target_os = "linux")]
                {
                    on_local_filesystem(holder_path) && without_access_acl(holder_path)
                }
                #[cfg(not(target_os = "linux"))]
                {
                    false
                }
            }
    };
    let distrust = posix_link_cause(me, meta.uid(), holder.uid(), holder.mode())
        .filter(|distrust| {
            *distrust != PosixDistrust::HolderWritable(Writer::Group)
                || !private_group(holder.gid())
        })
        .map(|distrust| match distrust {
            PosixDistrust::LinkOwner(owner) => Cause::LinkOwner {
                owner: account_name(owner),
                target: shown,
            },
            PosixDistrust::HolderOwner(owner) => Cause::HolderOwner {
                holder: holder_path.to_path_buf(),
                owner: account_name(owner),
                target: shown,
            },
            PosixDistrust::HolderWritable(writer) => Cause::HolderWritable {
                holder: holder_path.to_path_buf(),
                writer,
                owned: holder.uid() == me,
                target: shown,
            },
        });
    Ok(Entry::Link {
        target,
        distrust,
        identity: (meta.dev(), meta.ino()),
    })
}

#[cfg(unix)]
fn account_name(uid: u32) -> String {
    if uid == 0 {
        "root".into()
    } else {
        format!("the account with uid {uid}")
    }
}

#[cfg(windows)]
fn inspect(path: &Path) -> io::Result<Entry> {
    use crate::credentials::windows_security::{
        LinkDistrust, MarkerDistrust, PathEntry, inspect_entry,
    };
    let named = |distrust: &MarkerDistrust| match distrust {
        MarkerDistrust::Owner(owner) => (true, owner.clone()),
        MarkerDistrust::Writer { name, .. } => (false, name.clone()),
        MarkerDistrust::Unreadable => (false, "an entry this client cannot read".into()),
        MarkerDistrust::NoDacl => (false, "every account (no access list)".into()),
    };
    Ok(match inspect_entry(path)? {
        PathEntry::Missing | PathEntry::Plain | PathEntry::Placeholder => Entry::Plain,
        PathEntry::Other(tag) => Entry::Other(tag),
        PathEntry::Uncheckable(detail) => Entry::Link {
            target: PathBuf::new(),
            distrust: Some(Cause::UncheckableTarget { detail }),
            identity: (0, 0),
        },
        PathEntry::Link {
            target,
            distrust,
            identity,
        } => {
            let shown = target.clone();
            let distrust = distrust.map(|distrust| match distrust {
                LinkDistrust::Link(why) => match named(&why) {
                    (true, owner) => Cause::LinkOwner {
                        owner,
                        target: shown,
                    },
                    (false, writer) => Cause::LinkWriter {
                        writer,
                        target: shown,
                    },
                },
                LinkDistrust::Holder(holder, why) => match named(&why) {
                    (true, owner) => Cause::HolderOwner {
                        holder,
                        owner,
                        target: shown,
                    },
                    (false, writer) => Cause::HolderWritable {
                        holder,
                        writer: Writer::Account(writer),
                        owned: false,
                        target: shown,
                    },
                },
            });
            Entry::Link {
                target,
                distrust,
                identity,
            }
        }
    })
}

#[cfg(test)]
#[path = "private_path_tests.rs"]
mod tests;
