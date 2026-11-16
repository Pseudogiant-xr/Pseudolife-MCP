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

/// One check's state: the links already accepted (checked once each, so
/// nested links cost what they are, not what their prefixes repeat), the
/// links being resolved (one met again is a loop), and how many links the
/// check has followed.
#[derive(Default)]
struct Walk {
    accepted: HashSet<PathBuf>,
    resolving: HashSet<PathBuf>,
    links: usize,
}

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
        if self.accepted.contains(prefix) {
            return Ok(());
        }
        match inspect(prefix) {
            Ok(Entry::Plain) => Ok(()),
            Ok(Entry::Link { target, distrust }) => {
                if let Some(cause) = distrust {
                    return refuse(cause);
                }
                self.links += 1;
                if self.links > MAX_LINKS || !self.resolving.insert(prefix.to_path_buf()) {
                    return refuse(Cause::TooManyLinks);
                }
                // Where the link leads: absolute, or relative to the folder
                // holding it. The OS resolves `..` in what follows as it does
                // in the lookup itself, so the walk checks the very folders
                // the open will cross.
                let resolved = match prefix.parent() {
                    Some(holder) => holder.join(&target),
                    None => target.clone(),
                };
                self.walk(&resolved)?;
                self.resolving.remove(prefix);
                self.accepted.insert(prefix.to_path_buf());
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
    /// A link, where it leads, and why it is not trusted (if it is not).
    Link {
        target: PathBuf,
        distrust: Option<Cause>,
    },
    /// A Windows reparse point of another kind.
    #[cfg_attr(not(windows), allow(dead_code))]
    Other(u32),
}

#[cfg(unix)]
fn inspect(path: &Path) -> io::Result<Entry> {
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
    let distrust =
        posix_link_cause(me, meta.uid(), holder.uid(), holder.mode()).map(
            |distrust| match distrust {
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
            },
        );
    Ok(Entry::Link { target, distrust })
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
        },
        PathEntry::Link { target, distrust } => {
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
            Entry::Link { target, distrust }
        }
    })
}

#[cfg(test)]
#[path = "private_path_tests.rs"]
mod tests;
