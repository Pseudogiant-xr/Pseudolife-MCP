//! Whether the daemon account (uid 10001) can use the daemon's state and
//! model volumes, and the one-time repair when it cannot.
//!
//! The PROBE is a helper container from the daemon's own image, running as
//! 10001:10001 with no capabilities, no network and a read-only root
//! filesystem. It reads metadata only and writes nothing. It asks the
//! kernel's access check (find's `-readable`, `-writable`, `-executable`),
//! so it stays true where ownership is emulated (Docker Desktop bind mounts,
//! network filesystems). It reports what 10001 cannot use and cannot fix
//! itself: an entry another uid owns that 10001 cannot read, or a directory
//! it cannot enter or write. What 10001 owns but cannot use is only detail,
//! because the owner can change its own modes.
//!
//! The REPAIR runs only after a failed probe, over one part's mounts only.
//! First a uid-0 helper WITHOUT CHOWN lists every uid-0 entry with its
//! group, and that list is written to a private log: the undo list exists
//! before anything changes. Then the same image as uid 0 with only CHOWN and
//! [`SEARCH_CAP`] (DAC_OVERRIDE; both are needed, measured on Docker 29.9 on
//! 16 November 2026: with CHOWN alone a chowned 0700 directory can no longer
//! be traversed), no network and a read-only root filesystem gives those
//! entries to 10001:10001, following no link and entering no other mount:
//! it walks only the top-level state or model mount. Mounts the user added
//! below them are probed but never repaired.
//! A helper that fails or is interrupted is removed by name. The probe then
//! runs again, and a second failure refuses naming the path and the one fix.
//!
//! All three run `provision-daemon-models.sh` (the image entrypoint, a
//! byte-exact copy beside this file) with `/bin/sh -c`, so they work against
//! images published before the script had these modes. Nothing from the host
//! or the user is interpolated into the script: roots are separate argv
//! words. Answers come back hex-encoded, so no capture can alter a name.
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};

use serde_json::Value;

/// The entrypoint script, byte for byte (`ops/provision-daemon-models.sh`).
pub(crate) const SCRIPT: &str = include_str!("provision-daemon-models.sh");
/// `$0` of the script inside the helper.
const ARGV0: &str = "provision-daemon-models";
/// The daemon account of both daemon images.
pub(crate) const DAEMON_USER: &str = "10001:10001";
/// The capability that lets uid 0 enter the 0700 directories it does not
/// own (and the ones it has just given away) while listing and repairing.
/// DAC_OVERRIDE, not DAC_READ_SEARCH (maintainer ruling, 17 November 2026):
/// DAC_READ_SEARCH unlocks open_by_handle_at, which moby's default seccomp
/// profile allows once that capability is granted; DAC_OVERRIDE cannot
/// reach it and is a subset of what the release-N daemon already holds.
/// It also bypasses write permission, so the listing's "changes nothing"
/// rests on the script and on that helper having no CHOWN.
pub(crate) const SEARCH_CAP: &str = "DAC_OVERRIDE";
/// The state volume's mount point.
pub(crate) const STATE_DIR: &str = "/data";
/// The model cache's mount point when the container does not name one.
const MODELS_DIR: &str = "/models";

/// One mount the helper gets, at the daemon's own destination.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Mount {
    /// `volume`, `bind` or `tmpfs`.
    pub kind: String,
    /// The volume name or the host path; empty for tmpfs.
    pub source: String,
    pub destination: String,
    pub writable: bool,
    /// A volume mounted from a subdirectory of the volume.
    pub subpath: Option<String>,
    pub part: Part,
}

impl Mount {
    fn spec(&self) -> String {
        self.spec_as(self.writable)
    }
    fn spec_as(&self, writable: bool) -> String {
        let mut fields = vec![("type", self.kind.as_str())];
        match self.kind.as_str() {
            "volume" => fields.push(("src", self.source.as_str())),
            "bind" => fields.push(("source", self.source.as_str())),
            _ => {}
        }
        fields.push(("dst", self.destination.as_str()));
        if let Some(subpath) = &self.subpath {
            fields.push(("volume-subpath", subpath.as_str()));
        }
        if !writable && self.kind != "tmpfs" {
            fields.push(("readonly", "true"));
        }
        // Docker parses --mount as one CSV record.
        fields
            .iter()
            .map(|(key, value)| {
                let field = format!("{key}={value}");
                if field.contains([',', '"', '\n', '\r']) {
                    format!("\"{}\"", field.replace('"', "\"\""))
                } else {
                    field
                }
            })
            .collect::<Vec<_>>()
            .join(",")
    }
}

/// Which part of the daemon's storage a root belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Part {
    /// `/data` and mounts below it: what backups archive.
    State,
    /// The model cache: re-downloadable, outside backups.
    Models,
}

impl Part {
    fn noun(self) -> &'static str {
        match self {
            Part::State => "state volume",
            Part::Models => "model volume",
        }
    }
    fn word(self) -> &'static str {
        match self {
            Part::State => "state",
            Part::Models => "models",
        }
    }
}

/// The daemon container's storage as the helpers see it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Layout {
    /// The image the helpers run: the container's own, by ID, never pulled.
    pub image: String,
    /// The state and model mounts, in the container's order, plus its tmpfs
    /// mounts there (kept as boundaries the walks skip).
    pub mounts: Vec<Mount>,
    /// The top-level state and model mounts: the only roots the repair
    /// walks.
    pub roots: Vec<(String, Part)>,
    /// Writable mounts below them: the user's additions, probed and never
    /// repaired (an unusable one is refused with its source and the fix).
    pub nested: Vec<(String, Part)>,
}

fn below(path: &str, root: &str) -> bool {
    path == root
        || path
            .strip_prefix(root.trim_end_matches('/'))
            .is_some_and(|rest| rest.starts_with('/'))
}

/// The layout of one `docker inspect` object (a container, running or not).
/// None when it has no writable state mount at /data (nothing to probe).
pub(crate) fn layout(container: &Value) -> Result<Option<Layout>, String> {
    let models = container["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter_map(|entry| entry.strip_prefix("PSEUDOLIFE_DAEMON_MODELS_DIR="))
        .next_back()
        .filter(|dir| dir.starts_with('/'))
        .unwrap_or(MODELS_DIR);
    let models = match models.trim_end_matches('/') {
        "" => "/",
        trimmed => trimmed,
    }
    .to_owned();
    let part_of = |destination: &str| {
        if below(destination, STATE_DIR) {
            Some(Part::State)
        } else if models != "/" && below(destination, &models) {
            Some(Part::Models)
        } else {
            None
        }
    };
    // Subpaths are only in the configured mounts, by target.
    let subpath = |destination: &str| {
        container["HostConfig"]["Mounts"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|m| m["Target"] == destination && m["Type"] == "volume")
            .and_then(|m| m["VolumeOptions"]["Subpath"].as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
    };
    let mut mounts = Vec::new();
    let mut roots = Vec::new();
    let mut nested = Vec::new();
    for mount in container["Mounts"].as_array().into_iter().flatten() {
        let destination = mount["Destination"].as_str().unwrap_or_default();
        let Some(part) = part_of(destination) else {
            continue;
        };
        let kind = mount["Type"].as_str().unwrap_or_default();
        let writable = mount["RW"].as_bool().unwrap_or(true);
        if kind == "tmpfs" {
            mounts.push(Mount {
                kind: "tmpfs".into(),
                source: String::new(),
                destination: destination.to_owned(),
                writable,
                subpath: None,
                part,
            });
            continue;
        }
        let source = match kind {
            "volume" => mount["Name"].as_str(),
            "bind" => mount["Source"].as_str(),
            // A mount type the helpers cannot reproduce holds nothing ours.
            _ => continue,
        }
        .filter(|source| !source.is_empty())
        .ok_or_else(|| format!("cannot identify the source of the daemon's {destination} mount"))?;
        mounts.push(Mount {
            kind: kind.to_owned(),
            source: source.to_owned(),
            destination: destination.to_owned(),
            writable,
            subpath: if kind == "volume" {
                subpath(destination)
            } else {
                None
            },
            part,
        });
        // A read-only mount is never written by the daemon: the helpers keep
        // it (so the walks skip it as a mount) but do not walk it.
        if writable {
            if destination == STATE_DIR || destination == models {
                roots.push((destination.to_owned(), part));
            } else {
                nested.push((destination.to_owned(), part));
            }
        }
    }
    // `--tmpfs` mounts are listed only in the host configuration.
    if let Some(tmpfs) = container["HostConfig"]["Tmpfs"].as_object() {
        for destination in tmpfs.keys() {
            if let Some(part) = part_of(destination)
                && !mounts.iter().any(|m| &m.destination == destination)
            {
                mounts.push(Mount {
                    kind: "tmpfs".into(),
                    source: String::new(),
                    destination: destination.clone(),
                    writable: true,
                    subpath: None,
                    part,
                });
            }
        }
    }
    if !roots.iter().any(|(root, _)| root == STATE_DIR) {
        return Ok(None);
    }
    let image = container["Image"]
        .as_str()
        .filter(|image| !image.is_empty())
        .ok_or("cannot identify the daemon container's image")?
        .to_owned();
    Ok(Some(Layout {
        image,
        mounts,
        roots,
        nested,
    }))
}

impl Layout {
    /// The mount at `destination`.
    fn mount(&self, destination: &str) -> Option<&Mount> {
        self.mounts.iter().find(|m| m.destination == destination)
    }
    /// Whether `destination` is a top-level root.
    fn top(&self, destination: &str) -> bool {
        self.roots.iter().any(|(root, _)| root == destination)
    }
}

/// Which helper runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Helper {
    Probe,
    List(Part),
    Repair(Part),
}

/// A name for one helper container, so a failed or interrupted one can be
/// removed (`docker run --rm` removes it only once it exits by itself).
fn helper_name() -> String {
    format!("pseudolife-ownership-{}", uuid::Uuid::new_v4().simple())
}

fn helper_argv(layout: &Layout, helper: Helper, name: &str) -> Vec<String> {
    let mut argv: Vec<String> = ["run", "--rm", "--name", name, "--user"]
        .map(String::from)
        .to_vec();
    let (part, mode) = match helper {
        Helper::Probe => {
            argv.extend([DAEMON_USER, "--cap-drop", "ALL"].map(String::from));
            (None, "--probe-access")
        }
        Helper::List(part) => {
            argv.extend(["0:0", "--cap-drop", "ALL", "--cap-add", SEARCH_CAP].map(String::from));
            (Some(part), "--list-root-owned")
        }
        Helper::Repair(part) => {
            argv.extend(
                [
                    "0:0",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    SEARCH_CAP,
                ]
                .map(String::from),
            );
            (Some(part), "--repair-ownership")
        }
    };
    argv.extend(
        [
            "--security-opt",
            "no-new-privileges",
            "--network",
            "none",
            "--read-only",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--pull",
            "never",
            "--pids-limit",
            "256",
        ]
        .map(String::from),
    );
    // A uid-0 helper gets only its own part's mounts, and the user's nested
    // mounts there only read-only: it walks none of them.
    for mount in layout
        .mounts
        .iter()
        .filter(|m| part.is_none_or(|p| m.part == p))
    {
        let spec = if part.is_some() && !layout.top(&mount.destination) {
            mount.spec_as(false)
        } else {
            mount.spec()
        };
        argv.extend(["--mount".into(), spec]);
    }
    argv.extend([
        "--entrypoint".into(),
        "/bin/sh".into(),
        layout.image.clone(),
        "-c".into(),
        SCRIPT.into(),
        ARGV0.into(),
        mode.into(),
    ]);
    match part {
        // The probe walks every writable mount, nested ones included.
        None => argv.extend(
            layout
                .roots
                .iter()
                .chain(&layout.nested)
                .map(|(root, _)| root.clone()),
        ),
        Some(part) => argv.extend(
            layout
                .roots
                .iter()
                .filter(|(_, p)| *p == part)
                .map(|(root, _)| root.clone()),
        ),
    }
    argv
}

/// `docker run` arguments of the probe over every root.
pub(crate) fn probe_argv(layout: &Layout, name: &str) -> Vec<String> {
    helper_argv(layout, Helper::Probe, name)
}

/// `docker run` arguments of the listing before one part's repair.
pub(crate) fn list_argv(layout: &Layout, part: Part, name: &str) -> Vec<String> {
    helper_argv(layout, Helper::List(part), name)
}

/// `docker run` arguments of the repair over one part's roots.
pub(crate) fn repair_argv(layout: &Layout, part: Part, name: &str) -> Vec<String> {
    helper_argv(layout, Helper::Repair(part), name)
}

/// The helpers' hex answer, decoded. None when it is not hex.
fn decode(stdout: &str) -> Option<Vec<u8>> {
    let digits: Vec<u8> = stdout
        .bytes()
        .filter(|b| !b.is_ascii_whitespace())
        .collect();
    if !digits.len().is_multiple_of(2) {
        return None;
    }
    digits
        .chunks(2)
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok())
        .collect()
}

/// The NUL-separated fields of one complete helper answer: its header
/// `<mode> 1`, the records, and `end`. None for anything else (a truncated
/// answer, another program's output), which never counts as a result.
fn fields<'a>(answer: &'a [u8], mode: &str) -> Option<Vec<&'a [u8]>> {
    let body = answer.strip_suffix(b"\0")?;
    let mut fields: Vec<&[u8]> = body.split(|b| *b == 0).collect();
    if fields.len() < 3
        || fields[0] != mode.as_bytes()
        || fields[1] != b"1"
        || fields.pop()? != b"end"
    {
        return None;
    }
    Some(fields.split_off(2))
}

/// A path as one printable line: `\` `\t` `\n`, other control characters
/// and bytes that are not UTF-8 are escaped, so a name cannot forge a line.
pub(crate) fn shown(path: &[u8]) -> String {
    let mut out = String::with_capacity(path.len());
    for chunk in path.utf8_chunks() {
        for c in chunk.valid().chars() {
            match c {
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\t' => out.push_str("\\t"),
                c if c.is_control() => out.push_str(&format!("\\x{:02x}", c as u32)),
                c => out.push(c),
            }
        }
        for byte in chunk.invalid() {
            out.push_str(&format!("\\x{byte:02x}"));
        }
    }
    out
}

/// An entry 10001 cannot use and cannot fix.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Unusable {
    /// The owner's uid, or `?` for a root 10001 cannot reach.
    pub owner: String,
    pub path: Vec<u8>,
}

impl Unusable {
    pub fn shown(&self) -> String {
        shown(&self.path)
    }
}

/// What one probe saw.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Probe {
    pub unusable: Vec<Unusable>,
    /// Entries 10001 owns but cannot use: never refused or repaired.
    pub owner_fixable: Vec<Vec<u8>>,
    /// Roots 10001 owns but cannot read and enter: the probe cannot see in.
    pub unreadable_roots: Vec<Vec<u8>>,
}

impl Probe {
    /// The unusable entries below one part's top-level root (the ones the
    /// repair can reach).
    pub fn of(&self, layout: &Layout, part: Part) -> Vec<&Unusable> {
        self.unusable
            .iter()
            .filter(|entry| {
                root_of(layout, &entry.path).is_some_and(|(root, p)| p == part && layout.top(root))
            })
            .collect()
    }

    /// The unusable entries inside a nested mount, with that mount.
    pub fn nested<'a>(&'a self, layout: &'a Layout) -> Vec<(&'a Unusable, &'a Mount)> {
        self.unusable
            .iter()
            .filter_map(|entry| {
                let (root, _) = root_of(layout, &entry.path)?;
                (!layout.top(root)).then_some(())?;
                Some((entry, layout.mount(root)?))
            })
            .collect()
    }
}

/// The deepest walked root holding `path`, with its part.
fn root_of<'a>(layout: &'a Layout, path: &[u8]) -> Option<(&'a str, Part)> {
    let path = String::from_utf8_lossy(path);
    layout
        .roots
        .iter()
        .chain(&layout.nested)
        .filter(|(root, _)| below(&path, root))
        .max_by_key(|(root, _)| root.len())
        .map(|(root, part)| (root.as_str(), *part))
}

fn utf8(field: &[u8]) -> String {
    String::from_utf8_lossy(field).into_owned()
}

pub(crate) fn parse_probe(stdout: &str) -> Option<Probe> {
    let answer = decode(stdout)?;
    let fields = fields(&answer, "probe-access")?;
    let mut probe = Probe::default();
    let mut rest = fields.as_slice();
    while let Some((&tag, tail)) = rest.split_first() {
        match (tag, tail) {
            (b"U", [owner, path, tail @ ..]) => {
                probe.unusable.push(Unusable {
                    owner: utf8(owner),
                    path: path.to_vec(),
                });
                rest = tail;
            }
            (b"O", [path, tail @ ..]) => {
                probe.owner_fixable.push(path.to_vec());
                rest = tail;
            }
            (b"R", [path, tail @ ..]) => {
                probe.unreadable_roots.push(path.to_vec());
                rest = tail;
            }
            _ => return None,
        }
    }
    Some(probe)
}

/// One uid-0 entry: the group it had.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Visited {
    pub group: String,
    pub path: Vec<u8>,
}

/// The tagged records of a list or repair answer, and whether the answer
/// was complete (a partial one still yields its whole records).
fn parse_records(stdout: &str, mode: &str) -> (Vec<(u8, Visited)>, bool) {
    let Some(answer) = decode(stdout) else {
        return (Vec::new(), false);
    };
    let (body, complete): (Vec<&[u8]>, bool) = match fields(&answer, mode) {
        Some(fields) => (fields, true),
        None => {
            let mut all: Vec<&[u8]> = answer.split(|b| *b == 0).collect();
            if all.len() < 2 || all[0] != mode.as_bytes() || all[1] != b"1" {
                return (Vec::new(), false);
            }
            // Only whole records: the last field is unterminated.
            all.pop();
            (all.split_off(2), false)
        }
    };
    let mut records = Vec::new();
    for record in body.chunks(3) {
        match record {
            [tag, group, path] if tag.len() == 1 => records.push((
                tag[0],
                Visited {
                    group: utf8(group),
                    path: path.to_vec(),
                },
            )),
            _ => return (records, false),
        }
    }
    (records, complete)
}

/// What the helpers need from the command that runs them.
pub(crate) trait Docker {
    /// Runs `docker <args>`: the exit code and its output (standard output,
    /// or Docker's own error text when it printed nothing else).
    fn docker(&mut self, args: &[String]) -> (i32, String);
    /// Removes a helper by name, also after an interrupt.
    fn remove(&mut self, name: &str) {
        let _ = self.docker(&["rm".into(), "-f".into(), "-v".into(), name.into()]);
    }
}

/// The text of one refusal or failure; the caller adds its own tail
/// ("nothing was deployed", "Nothing was restored").
pub(crate) type Refusal = String;

/// The root an exit-71 walk names in its last `E` record.
fn failed_root(output: &str) -> Option<String> {
    let answer = decode(output)?;
    let fields: Vec<&[u8]> = answer.split(|b| *b == 0).collect();
    let at = fields.iter().rposition(|f| *f == b"E")?;
    fields.get(at + 1).map(|root| shown(root))
}

/// Docker's own first line, when the helper printed no answer.
fn said(output: &str) -> String {
    match output.lines().map(str::trim).find(|l| !l.is_empty()) {
        Some(line) if decode(output).is_none() => format!(": {line}"),
        _ => String::new(),
    }
}

fn walk_failure(code: i32, output: &str, image: &str) -> String {
    match code {
        125 => format!(
            "Docker could not start the ownership helper from the daemon's image {image} (docker exited 125{}); fix what Docker reports and retry",
            said(output)
        ),
        126 | 127 => format!(
            "the daemon's image {image} could not run /bin/sh for the ownership helper (docker exited {code}{}); restore that image (update or reinstall the daemon) and retry",
            said(output)
        ),
        70 => "the ownership helper could not read its mount table (/proc/self/mountinfo); check that Docker mounts /proc in containers and retry".into(),
        71 => match failed_root(output) {
            Some(root) => format!(
                "the ownership helper could not read everything below {root} (find was refused there: its permissions, or a label such as SELinux's that the helper does not carry); make it readable and searchable inside the daemon's container, then retry"
            ),
            None => "the ownership helper's walk of the daemon's volumes failed partway; check the volumes' filesystem, then retry".into(),
        },
        72 => "a volume the daemon mounts is missing from the ownership helper's view; recreate the daemon container from its Compose project and retry".into(),
        // It exited 0 but its answer was not the records it writes.
        -1 => format!(
            "the ownership helper's answer was incomplete or unreadable; check that the daemon's image {image} has GNU find and od, then retry"
        ),
        _ => format!(
            "the ownership helper exited {code}{}; check `docker info` and that the image {image} still exists, then retry",
            said(output)
        ),
    }
}

/// Runs one helper; removes it by name unless it finished cleanly.
fn run(docker: &mut impl Docker, argv: impl FnOnce(&str) -> Vec<String>) -> (i32, String, bool) {
    let name = helper_name();
    let (code, output) = docker.docker(&argv(&name));
    let finished = code == 0;
    if !finished {
        docker.remove(&name);
    }
    (code, output, finished)
}

/// Runs the probe. A probe that could not run is never "usable".
pub(crate) fn probe(docker: &mut impl Docker, layout: &Layout) -> Result<Probe, Refusal> {
    let (code, output, finished) = run(docker, |name| probe_argv(layout, name));
    if finished && let Some(probe) = parse_probe(&output) {
        return Ok(probe);
    }
    let code = if finished { -1 } else { code };
    Err(format!(
        "cannot check whether the daemon account can use its volumes: {}",
        walk_failure(code, &output, &layout.image)
    ))
}

/// What a repair changed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Repaired {
    pub part: Part,
    pub changed: usize,
    /// The undo list; None when there was nothing root-owned to change.
    pub log: Option<PathBuf>,
}

impl Repaired {
    /// The one line a user sees.
    pub fn line(&self) -> String {
        match &self.log {
            Some(log) if self.changed > 0 => format!(
                "gave {} root-owned entr{} in the daemon's {} to its account (10001:10001) so it no longer needs root; the list of what changed is {}",
                self.changed,
                if self.changed == 1 { "y" } else { "ies" },
                self.part.noun(),
                log.display()
            ),
            _ => format!(
                "the daemon's {} needed no ownership change",
                self.part.noun()
            ),
        }
    }
}

fn private_file(path: &Path) -> std::io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    options.open(path)
}

/// The undo list, written before anything changes.
fn write_log(
    directory: &Path,
    stamp: &str,
    layout: &Layout,
    part: Part,
    entries: &[Visited],
) -> std::io::Result<(PathBuf, fs::File)> {
    fs::create_dir_all(directory)?;
    // Unique even for two repairs in one second (a per-second stamp).
    let token = uuid::Uuid::new_v4().simple().to_string();
    let path = directory.join(format!(
        "ownership-repair-{stamp}-{}-{}.log",
        &token[..8],
        part.word()
    ));
    let mut text = format!(
        "# Pseudolife ownership repair of the daemon's {}, image {}.\n# Written before the repair: every line is one root-owned entry it gives to\n# 10001:10001, as owner:group before, a tab, and its path inside the daemon\n# container (\\\\ \\t \\n and \\xHH escape special bytes). Setting each entry\n# back to its old owner and group undoes the repair.\n",
        part.noun(),
        layout.image
    );
    for mount in layout
        .mounts
        .iter()
        .filter(|m| m.part == part && m.kind != "tmpfs")
    {
        text.push_str(&format!(
            "# mount: {} {} at {}\n",
            mount.kind,
            shown(mount.source.as_bytes()),
            shown(mount.destination.as_bytes())
        ));
    }
    for entry in entries {
        text.push_str(&format!("0:{}\t{}\n", entry.group, shown(&entry.path)));
    }
    let mut file = private_file(&path)?;
    file.write_all(text.as_bytes())?;
    file.sync_all()?;
    Ok((path, file))
}

/// The refusal for entries the repair cannot fix.
fn unfixable(entries: &[&Unusable], part: Part, log: Option<&Path>) -> Refusal {
    let first = entries[0];
    let more = if entries.len() > 1 {
        format!(" (and {} more)", entries.len() - 1)
    } else {
        String::new()
    };
    let owner = match first.owner.as_str() {
        "0" => "root still owns it".to_owned(),
        "?" => "it is missing or below a directory that account cannot search".to_owned(),
        other => format!("uid {other} owns it"),
    };
    let done = match log {
        Some(log) => format!(
            "; the repair changed only the owners listed in {}, and nothing else was changed",
            log.display()
        ),
        None => "; nothing else was changed".into(),
    };
    format!(
        "the daemon account (uid 10001) cannot use {}{more} in the daemon's {}: {owner}, and the ownership repair changes only root-owned entries on a filesystem that allows it. Give it to 10001:10001 (for a bind mount, change the owner of its host directory; on a network filesystem, on its server), then retry{done}",
        first.shown(),
        part.noun(),
    )
}

/// The refusal for a root the probe cannot see into.
fn unreadable_root(path: &[u8]) -> Refusal {
    format!(
        "the daemon account (uid 10001) owns {} but cannot read or enter it, so nothing below it can be checked: its permissions, or a label such as SELinux's (relabel a bind mount with :z) the check does not carry. Give that account read and search permission on it, then retry; nothing was changed",
        shown(path)
    )
}

/// The refusal for an unusable entry in a mount the user added.
fn nested_unusable(entry: &Unusable, mount: &Mount) -> Refusal {
    let (what, fix) = if mount.kind == "bind" {
        (
            format!("the host directory {}", shown(mount.source.as_bytes())),
            format!(
                "change the owner of {} on the host to 10001:10001",
                shown(mount.source.as_bytes())
            ),
        )
    } else {
        (
            format!("the volume {}", shown(mount.source.as_bytes())),
            "give its entries to 10001:10001 inside that volume".to_owned(),
        )
    };
    format!(
        "the daemon account (uid 10001) cannot use {} in {}, mounted at {} below the daemon's {}: the ownership repair leaves mounts added below it alone. To fix it, {fix}, then retry; nothing was changed",
        entry.shown(),
        what,
        shown(mount.destination.as_bytes()),
        mount.part.noun()
    )
}

/// Whether the engine runs rootless or remaps user namespaces (uid 0 in a
/// container is then not root on the host). None when Docker cannot say.
fn remapped(docker: &mut impl Docker) -> Option<bool> {
    let (code, output) = docker.docker(&[
        "info".into(),
        "--format".into(),
        "{{json .SecurityOptions}}".into(),
    ]);
    let options: Vec<String> = serde_json::from_str(output.trim())
        .ok()
        .filter(|_| code == 0)?;
    Some(
        options
            .iter()
            .any(|o| o.contains("name=rootless") || o.contains("name=userns")),
    )
}

/// Repairs one part after a probe found it unusable, then probes again.
/// `log_dir` receives the undo list, before the change. Ok only when the
/// second probe finds that part usable.
pub(crate) fn repair(
    docker: &mut impl Docker,
    layout: &Layout,
    part: Part,
    log_dir: &Path,
    stamp: &str,
) -> Result<Repaired, Refusal> {
    // A host directory as the root on a rootless or user-remapped engine:
    // uid 0 there is not root on the host, so the repair would hand host
    // files to an unrelated uid. Detected, never guessed.
    if let Some((root, _)) = layout.roots.iter().find(|(_, p)| *p == part)
        && let Some(mount) = layout.mount(root).filter(|m| m.kind == "bind")
        && remapped(docker) != Some(false)
    {
        return Err(format!(
            "the daemon's {} is the host directory {}, and this Docker engine runs rootless or remaps user namespaces (or could not say which), where uid 0 in a container is not root on the host, so the ownership repair does not run there. Change the owner of {} on the host so the daemon account (uid 10001 in the container) can use it, then retry; nothing was changed",
            shown(root.as_bytes()),
            shown(mount.source.as_bytes()),
            shown(mount.source.as_bytes())
        ));
    }
    // 1. What root owns, listed by a uid-0 helper that cannot chown.
    let (code, output, finished) = run(docker, |name| list_argv(layout, part, name));
    let (listed, complete) = parse_records(&output, "list-root-owned");
    if !finished || !complete || listed.iter().any(|(tag, _)| *tag != b'L') {
        return Err(format!(
            "cannot list what root owns in the daemon's {} before repairing it, so nothing was changed: {}",
            part.noun(),
            walk_failure(if finished { -1 } else { code }, &output, &layout.image)
        ));
    }
    let listed: Vec<Visited> = listed.into_iter().map(|(_, v)| v).collect();
    let mut log = None;
    if !listed.is_empty() {
        // 2. The undo list, durable before the first change.
        let (path, mut file) = write_log(log_dir, stamp, layout, part, &listed).map_err(|error| {
            format!(
                "cannot write the list of the {} root-owned entries in the daemon's {} under {} ({error}), so nothing was changed; make that directory writable and retry",
                listed.len(),
                part.noun(),
                log_dir.display()
            )
        })?;
        // 3. The change.
        let (code, output, finished) = run(docker, |name| repair_argv(layout, part, name));
        let (records, complete) = parse_records(&output, "repair-ownership");
        // Anything the listing missed (created since) is appended; a failed
        // chown is kept as a comment.
        let mut extra = String::new();
        for (tag, entry) in &records {
            if *tag == b'C' && !listed.iter().any(|l| l.path == entry.path) {
                extra.push_str(&format!("0:{}\t{}\n", entry.group, shown(&entry.path)));
            } else if *tag == b'F' {
                extra.push_str(&format!("# not changed: {}\n", shown(&entry.path)));
            }
        }
        if !extra.is_empty() {
            let _ = file
                .write_all(extra.as_bytes())
                .and_then(|()| file.sync_all());
        }
        if !finished || !complete {
            return Err(format!(
                "the ownership repair of the daemon's {} did not finish, so some of the entries listed in {} may still be root-owned: {}",
                part.noun(),
                path.display(),
                walk_failure(if finished { -1 } else { code }, &output, &layout.image)
            ));
        }
        log = Some((path, records.iter().filter(|(t, _)| *t == b'C').count()));
    }
    let repaired = Repaired {
        part,
        changed: log.as_ref().map_or(0, |(_, n)| *n),
        log: log.map(|(path, _)| path),
    };
    // 4. Probe again.
    let after = probe(docker, layout)?;
    if let Some(root) = after.unreadable_roots.first() {
        return Err(unreadable_root(root));
    }
    let left = after.of(layout, part);
    if !left.is_empty() {
        return Err(unfixable(&left, part, repaired.log.as_deref()));
    }
    Ok(repaired)
}

/// The first half of the order rule, before anything that needs the state:
/// probe; repair the model cache if it needs it (a cache outside backups,
/// so nothing backed up is at stake). What the state needs is left to the
/// caller, which backs up first when a container can read the old state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Preflight {
    pub layout: Layout,
    /// What 10001 cannot use in the state volume; empty when it is usable.
    pub state: Vec<Unusable>,
    pub models: Option<Repaired>,
}

impl Preflight {
    pub fn state_usable(&self) -> bool {
        self.state.is_empty()
    }
}

/// Probes `container` (its `docker inspect` object) and repairs its model
/// cache when needed. None when the container has no state mount at /data.
/// `admit` sees what the state needs before anything is changed and may
/// refuse (a caller that cannot back up first).
pub(crate) fn preflight(
    docker: &mut impl Docker,
    container: &Value,
    log_dir: &Path,
    stamp: &str,
    admit: impl FnOnce(&[&Unusable]) -> Result<(), Refusal>,
) -> Result<Option<Preflight>, Refusal> {
    let Some(layout) = layout(container)? else {
        return Ok(None);
    };
    let first = probe(docker, &layout)?;
    if let Some(root) = first.unreadable_roots.first() {
        return Err(unreadable_root(root));
    }
    if let Some((entry, mount)) = first.nested(&layout).first() {
        return Err(nested_unusable(entry, mount));
    }
    admit(&first.of(&layout, Part::State))?;
    let models = if first.of(&layout, Part::Models).is_empty() {
        None
    } else {
        Some(repair(docker, &layout, Part::Models, log_dir, stamp)?)
    };
    let state = first
        .of(&layout, Part::State)
        .into_iter()
        .cloned()
        .collect();
    Ok(Some(Preflight {
        layout,
        state,
        models,
    }))
}

/// Whether the container's configured user is root: then an exec into it
/// can read root-owned state. Docker's default user is root.
pub(crate) fn runs_as_root(container: &Value) -> bool {
    let user = container["Config"]["User"].as_str().unwrap_or_default();
    let name = user.split(':').next().unwrap_or_default();
    name.is_empty() || name == "root" || name == "0"
}

#[cfg(test)]
mod tests;
