//! The vendor runtime: OpenAI's `tunnel-client` release, downloaded at a
//! pinned version, verified against a digest compiled into this client,
//! unpacked flat into the tunnel store and probed before it is installed.
//!
//! Trusted for a download: only the `PINS` table below (an archive's exact
//! size and sha256). Not trusted: the network, the download's length and
//! bytes until they match a pin, and every piece of the archive's own
//! metadata (names, types, declared sizes, permissions), which is checked
//! even after the digest matches. A rerun reuses an installed copy on the
//! strength of the owner-only tunnel store: its `checksums.json`, written
//! from a verified archive, is checked against the files, so corruption, a
//! planted file or a swapped link is refused, but a program running as this
//! account could rewrite both, as it could the tunnel key beside them.
//!
//! Assumptions about the archive, each checked; any violation refuses with
//! nothing written:
//! - a zip of at most `MEMBER_LIMIT` members, stored or deflated, none
//!   encrypted, its local headers agreeing with its central directory;
//! - every member a regular file at the top level, its name made of ASCII
//!   letters, digits, `.`, `_` and `-`, not starting with a dot, not a
//!   Windows device name, and unique without regard to case (no directory
//!   entries, links or special files: a zip can carry a symlink but cannot
//!   express a hard link or a junction, so those are refused on disk);
//! - one member is the wrapper, `tunnel-client` (`tunnel-client.exe` on
//!   Windows), and none is named `checksums.json`, the manifest written here;
//! - the members unpack to at most `UNPACKED_LIMIT` bytes in all.
//!
//! The installed layout is the frozen Python client's
//! (`tunnel-client-v<version>-<system>-<arch>/` holding the members and a
//! `checksums.json` of `{name: sha256}`), so a runtime either client
//! installed is reused by the other.
#![allow(dead_code, reason = "T6b's supervisor is the first caller of ensure")]

use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    io::{self, Cursor, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

use sha2::{Digest, Sha256};

use super::store;

pub(super) const VERSION: &str = "0.0.15";

/// One release archive: the platform in the vendor's spelling, its exact
/// size in bytes and its sha256.
#[derive(Clone, Copy, Debug)]
pub(super) struct Pin<'a> {
    pub system: &'a str,
    pub arch: &'a str,
    pub size: u64,
    pub sha256: &'a str,
}

/// The `tunnel-client-v{VERSION}-<system>-<arch>.zip` assets of the
/// `openai/tunnel-client` GitHub release `v{VERSION}`. The digests are the
/// frozen Python client's (`tunnel_runtime.py` `ASSETS`); on 16 November
/// 2026 each equalled both the digest GitHub reports for the asset and the
/// sha256 of a fresh download, and the sizes are those downloads' lengths.
/// To move to a new version: download each zip with `gh release download`,
/// check its sha256 against the release's `SHA256SUMS.txt` and the digest
/// GitHub lists for the asset, and replace `VERSION` and all six rows in one
/// change; list its members first, since the assumptions above must still
/// hold.
pub(super) const PINS: [Pin<'static>; 6] = [
    Pin {
        system: "darwin",
        arch: "amd64",
        size: 28_858_580,
        sha256: "9dcae1e2fb121287e73271edb7b853dda52aa86b7bfca1df91bc275371261bdb",
    },
    Pin {
        system: "darwin",
        arch: "arm64",
        size: 27_018_754,
        sha256: "b2cae3aa9df45b4c2fe9b1d700ebacce39f9feb6a6b46b86e6499f9a51bf72ff",
    },
    Pin {
        system: "linux",
        arch: "amd64",
        size: 28_021_257,
        sha256: "8c836dc5d68d68b663d9a5c5b28ff9fa780d9f7a3fffb1c306880b8f32fab5f1",
    },
    Pin {
        system: "linux",
        arch: "arm64",
        size: 25_588_737,
        sha256: "c51bfd883fc22e3445494a03c0179875176564bde470661b308fd83af5d01abb",
    },
    Pin {
        system: "windows",
        arch: "amd64",
        size: 28_098_744,
        sha256: "3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1",
    },
    Pin {
        system: "windows",
        arch: "arm64",
        size: 25_474_435,
        sha256: "571e0d59ed9e86d1b105dc34f3267865f654de6968b01efd7c847f0af657d11d",
    },
];

/// The v0.0.15 archives, listed on 16 November 2026, hold 7 members each and
/// unpack to at most 63,713,911 bytes (darwin-amd64; its largest member,
/// `cloudflared`, is 40,880,256). The cap is about 1.5 times that, so a
/// bomb is bounded while a patch release's growth needs no change here.
const UNPACKED_LIMIT: u64 = 96 * 1024 * 1024;
/// The same archives' 7 members, with room for a notice file or two more.
const MEMBER_LIMIT: usize = 16;
const MANIFEST: &str = "checksums.json";
const MANIFEST_LIMIT: usize = 64 * 1024;
/// How long `tunnel-client --help` may take (the Python client's bound).
const PROBE_TIMEOUT: Duration = Duration::from_secs(15);
/// The whole download's bound: the largest pinned archive (28.9 MB) in 15
/// minutes needs about 32 KB/s.
const TRANSFER_TIMEOUT: Duration = Duration::from_secs(900);
/// A connection that cannot be made, or a transfer that stalls, fails long
/// before the whole bound (the Python client allowed 60 s per operation).
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const READ_TIMEOUT: Duration = Duration::from_secs(60);

/// The only environment the vendor program sees: the frozen Python client's
/// launch allowlist (`child_environment`), so no token or API key in the
/// caller's environment reaches it. The supervisor's real launch (T6b) must
/// use the same rule.
const PROBE_ENVIRONMENT: [&str; 15] = [
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
];
/// The probe's own exit codes for outcomes the program did not choose.
const PROBE_TIMED_OUT: i32 = 124;
const PROBE_NOT_STARTED: i32 = 126;

/// How the downloaded program is checked: `program --help` run in `cwd` with
/// exactly `env`, answering its exit code (`PROBE_TIMED_OUT`,
/// `PROBE_NOT_STARTED`, or 128 + n for signal n).
pub(super) trait Probe {
    fn help(&self, program: &Path, cwd: &Path, env: &[(OsString, OsString)]) -> i32;
}

/// The real probe: stdin, stdout and stderr closed, no window of its own
/// when this process has none, and the program killed at `PROBE_TIMEOUT`.
/// `--help` starts no children, so the program alone is stopped.
struct SystemProbe;
impl Probe for SystemProbe {
    fn help(&self, program: &Path, cwd: &Path, env: &[(OsString, OsString)]) -> i32 {
        let mut command = Command::new(program);
        command
            .arg("--help")
            .current_dir(cwd)
            .env_clear()
            .envs(env.iter().map(|(key, value)| (key, value)))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(crate::cli::restore_cli::child_window::child_window_flag());
        }
        let Ok(mut child) = command.spawn() else {
            return PROBE_NOT_STARTED;
        };
        let deadline = Instant::now() + PROBE_TIMEOUT;
        loop {
            match child.try_wait() {
                Ok(Some(status)) => return exit_code(status),
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(50))
                }
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return PROBE_TIMED_OUT;
                }
            }
        }
    }
}

fn exit_code(status: std::process::ExitStatus) -> i32 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return 128 + signal;
        }
    }
    status.code().unwrap_or(1)
}

/// The allowlisted part of `vars` (names compared without case on Windows,
/// where the environment ignores it).
pub(super) fn probe_environment(
    vars: impl IntoIterator<Item = (OsString, OsString)>,
) -> Vec<(OsString, OsString)> {
    vars.into_iter()
        .filter(|(key, _)| {
            key.to_str().is_some_and(|key| {
                PROBE_ENVIRONMENT.iter().any(|allowed| {
                    if cfg!(windows) {
                        allowed.eq_ignore_ascii_case(key)
                    } else {
                        *allowed == key
                    }
                })
            })
        })
        .collect()
}
/// An interrupted install (a kill, a closed terminal, Ctrl-C during the
/// probe) leaves its stage behind. An install in progress writes into its
/// stage within seconds and holds it for at most the probe after that, so a
/// stage untouched for an hour is a leftover the next install removes.
const STALE_STAGE: Duration = Duration::from_secs(3600);

/// What `ensure` left in place: the wrapper to run, and whether this call
/// installed it (`false`: a verified runtime was already there).
#[derive(Debug)]
pub(super) struct Installed {
    pub binary: PathBuf,
    pub changed: bool,
}
impl Installed {
    pub(super) fn summary(&self) -> String {
        let folder = self.binary.parent().unwrap_or(&self.binary).display();
        if self.changed {
            format!("Installed the tunnel runtime {VERSION} in {folder} (checksum verified).")
        } else {
            format!(
                "The tunnel runtime {VERSION} in {folder} is already installed and verified; nothing changed."
            )
        }
    }
}

/// The vendor's spelling of a Rust `(OS, ARCH)` pair, for the pinned
/// platforms only.
fn asset_platform(os: &str, arch: &str) -> Option<(&'static str, &'static str)> {
    let system = match os {
        "linux" => "linux",
        "macos" => "darwin",
        "windows" => "windows",
        _ => return None,
    };
    let arch = match arch {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        _ => return None,
    };
    Some((system, arch))
}

/// The pin for a Rust `(OS, ARCH)` pair, or a refusal naming the platform.
fn pin_for(os: &str, arch: &str) -> Result<Pin<'static>, String> {
    asset_platform(os, arch)
        .and_then(|(system, arch)| {
            PINS.iter()
                .find(|pin| pin.system == system && pin.arch == arch)
                .copied()
        })
        .ok_or_else(|| {
            format!(
                "the tunnel runtime (OpenAI tunnel-client {VERSION}) has no release for this platform ({os}-{arch}); run the tunnel on Linux, macOS or Windows on x86-64 or ARM64"
            )
        })
}

fn binary_name(pin: &Pin) -> &'static str {
    if pin.system == "windows" {
        "tunnel-client.exe"
    } else {
        "tunnel-client"
    }
}

fn folder_name(pin: &Pin) -> String {
    format!("tunnel-client-v{VERSION}-{}-{}", pin.system, pin.arch)
}

fn url(pin: &Pin) -> String {
    format!(
        "https://github.com/openai/tunnel-client/releases/download/v{VERSION}/{}.zip",
        folder_name(pin)
    )
}

/// Make sure the pinned runtime for this host is installed under the tunnel
/// store `root` and return its wrapper, downloading it only when no verified
/// copy is there.
pub(super) fn ensure(root: &Path) -> Result<Installed, String> {
    let pin = pin_for(std::env::consts::OS, std::env::consts::ARCH)?;
    ensure_with(root, pin, &SystemProbe, download)
}

/// `ensure` with its effects injected: the pin, the `--help` probe and the
/// download (`fetch(url, exact size)`).
fn ensure_with(
    root: &Path,
    pin: Pin,
    probe: &dyn Probe,
    fetch: impl FnOnce(&str, u64) -> Result<Vec<u8>, String>,
) -> Result<Installed, String> {
    prepare_root(root)?;
    // Before reuse too: a stage an interrupted install left beside a
    // runtime that later installed would otherwise never go.
    sweep_stale_stages(root);
    let target = root.join(folder_name(&pin));
    if let Some(installed) = installed(&target, &pin)? {
        return Ok(installed);
    }
    let archive = fetch(&url(&pin), pin.size)?;
    install(root, pin, &archive, probe, UNPACKED_LIMIT)
}

/// Remove stages earlier installs left (`STALE_STAGE`): only an owner-only
/// plain folder named exactly as an install names its stage (this client's
/// `.runtime-<32 lowercase hex>`, or the Python client's `mkdtemp`
/// `.runtime-<8 of a-z, 0-9, _>`) that holds only regular files an install
/// writes. Anything else is left as it is; a failure here never stops the
/// install.
fn sweep_stale_stages(root: &Path) {
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(suffix) = name.to_str().and_then(|n| n.strip_prefix(".runtime-")) else {
            continue;
        };
        let uuid = suffix.len() == 32
            && suffix
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        let mkdtemp = suffix.len() == 8
            && suffix
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
        if !uuid && !mkdtemp {
            continue;
        }
        let stage = entry.path();
        let Ok(info) = fs::symlink_metadata(&stage) else {
            continue;
        };
        let stale = info
            .modified()
            .ok()
            .and_then(|at| at.elapsed().ok())
            .is_some_and(|age| age >= STALE_STAGE);
        if !info.is_dir() || !stale || store::owner_only_directory(&stage, &info).is_err() {
            continue;
        }
        let Ok(listing) = fs::read_dir(&stage) else {
            continue;
        };
        let Ok(files) = listing.collect::<io::Result<Vec<_>>>() else {
            continue;
        };
        let expected = files.iter().all(|file| {
            file.file_type().is_ok_and(|kind| kind.is_file())
                && file
                    .file_name()
                    .to_str()
                    .is_some_and(|n| n == MANIFEST || member_problem(n).is_none())
        });
        if !expected {
            continue;
        }
        for file in &files {
            remove_retry(|| fs::remove_file(file.path()));
        }
        remove_retry(|| fs::remove_dir(&stage));
    }
}

/// The store root: an owner-only plain folder, reached only through links
/// the client's one private-path rule accepts (`board::state::private_dir`,
/// over `crate::private_path`: a link nobody but this user could have made or
/// can change).
fn prepare_root(root: &Path) -> Result<(), String> {
    let refuse = || {
        format!(
            "the tunnel directory {} is not a plain folder only your account can access, or a link on its path could be changed by another account; make it one, then retry",
            root.display()
        )
    };
    // The root itself is never a link or junction, trusted or not: the
    // tunnel store's other readers (`store::checked_path`) refuse one there,
    // and `private_dir`, which follows a trusted one, must not change what it
    // leads to.
    if fs::symlink_metadata(root).is_ok_and(|info| !info.is_dir()) {
        return Err(refuse());
    }
    crate::board::state::private_dir(root).map_err(|_| refuse())?;
    let info = fs::symlink_metadata(root).map_err(|_| refuse())?;
    if !info.is_dir() {
        return Err(refuse());
    }
    store::owner_only_directory(root, &info).map_err(|_| refuse())
}

/// A verified runtime at `target`, `None` when nothing is there, or a
/// refusal when something is there that is not one.
fn installed(target: &Path, pin: &Pin) -> Result<Option<Installed>, String> {
    match fs::symlink_metadata(target) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(corrupt(target, "it cannot be inspected")),
        Ok(_) => {}
    }
    verify(target, pin).map_err(|why| corrupt(target, why))?;
    Ok(Some(Installed {
        binary: target.join(binary_name(pin)),
        changed: false,
    }))
}

fn corrupt(target: &Path, why: &str) -> String {
    format!(
        "the installed tunnel runtime at {} failed its integrity check ({why}); stop any tunnel that uses it, move that folder aside, then retry",
        target.display()
    )
}

/// Check an installed runtime: an owner-only plain folder holding exactly
/// its manifest and the files the manifest names, each owner-only, with one
/// link, and matching its recorded digest.
fn verify(target: &Path, pin: &Pin) -> Result<(), &'static str> {
    let info = fs::symlink_metadata(target).map_err(|_| "it cannot be inspected")?;
    if !info.is_dir() {
        return Err("it is not a plain folder");
    }
    store::owner_only_directory(target, &info).map_err(|_| "the folder is not owner-only")?;
    let bytes = store::private_read(&target.join(MANIFEST), MANIFEST_LIMIT)
        .map_err(|_| "its checksums.json is missing, not owner-only or a link")?;
    let manifest: BTreeMap<String, String> =
        serde_json::from_slice(&bytes).map_err(|_| "its checksums.json is invalid")?;
    if !manifest.contains_key(binary_name(pin))
        || manifest
            .iter()
            .any(|(name, digest)| member_problem(name).is_some() || !is_digest(digest))
    {
        return Err("its checksums.json is invalid");
    }
    let mut present = BTreeSet::new();
    for entry in fs::read_dir(target).map_err(|_| "it cannot be listed")? {
        let name = entry.map_err(|_| "it cannot be listed")?.file_name();
        present.insert(
            name.into_string()
                .map_err(|_| "it holds an unexpected file")?,
        );
    }
    let expected: BTreeSet<String> = manifest
        .keys()
        .cloned()
        .chain([MANIFEST.to_owned()])
        .collect();
    if present != expected {
        return Err("it holds files its checksums.json does not name, or lacks some");
    }
    for (name, digest) in &manifest {
        // Never blocks: a FIFO in place of a file is refused, not opened.
        let data = store::private_read(&target.join(name), UNPACKED_LIMIT as usize)
            .map_err(|_| "a file in it is not an owner-only regular file with one link")?;
        if format!("{:x}", Sha256::digest(&data)) != *digest {
            return Err("a file in it does not match its recorded checksum");
        }
    }
    Ok(())
}

fn is_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Why `name` cannot be a member, or `None` when it is a plain top-level
/// name (the allow-list in the module comment).
fn member_problem(name: &str) -> Option<&'static str> {
    if name.contains(['/', '\\']) {
        return Some("it is in a folder, or names an absolute path");
    }
    if name.contains(':') {
        return Some("it names a drive or an alternate data stream");
    }
    if name == "." || name == ".." || name.starts_with('.') {
        return Some("it is hidden or a parent reference");
    }
    if name.is_empty()
        || name.len() > 128
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    {
        return Some("its name has characters outside letters, digits, '.', '_' and '-'");
    }
    let stem = name.split('.').next().unwrap_or(name).to_ascii_uppercase();
    let device = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && stem.as_bytes()[3].is_ascii_digit());
    if device {
        return Some("it is a Windows device name");
    }
    if name.ends_with('.') {
        return Some("Windows would drop its trailing dot");
    }
    None
}

/// Install `archive` (the download) under `root`: exact size, digest, the
/// archive checks, a flat owner-only stage, the probe, then one rename into
/// place. A refusal leaves nothing this call wrote.
fn install(
    root: &Path,
    pin: Pin,
    archive: &[u8],
    probe: &dyn Probe,
    unpacked_limit: u64,
) -> Result<Installed, String> {
    let got = archive.len() as u64;
    if got < pin.size {
        return Err(format!(
            "the tunnel runtime download ended early ({got} of {} bytes); nothing was installed; retry, and if this repeats and the host reaches the internet through a proxy or a sign-in portal, check that it passes downloads from github.com unchanged",
            pin.size
        ));
    }
    if got > pin.size {
        return Err(format!(
            "the tunnel runtime download is larger than the pinned release ({got} bytes, expected {}); nothing was installed; if the host reaches the internet through a proxy or a sign-in portal, check that it passes downloads from github.com unchanged, then retry",
            pin.size
        ));
    }
    let digest = format!("{:x}", Sha256::digest(archive));
    if digest != pin.sha256 {
        return Err(format!(
            "the tunnel runtime download does not match the checksum this client pins (sha256 {digest}, expected {}); nothing was installed; retry on a network you trust, and report it if it repeats",
            pin.sha256
        ));
    }
    let mut zip = zip::ZipArchive::new(Cursor::new(archive))
        .map_err(|_| unsafe_archive("it is not a readable zip"))?;
    let members = members(&mut zip, &pin, unpacked_limit)?;
    let stage = root.join(format!(".runtime-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        crate::board::state::private_dir(&stage)
            .map_err(|_| unwritable(root, "its folder cannot be created"))?;
        let mut manifest = BTreeMap::new();
        let mut total = 0u64;
        for (index, name) in members.iter().enumerate() {
            let path = stage.join(name);
            let mut opened = crate::board::state::create(&path)
                .map_err(|_| unwritable(root, "a file cannot be created"))?;
            let entry = zip
                .by_index(index)
                .map_err(|_| unsafe_archive("a member cannot be read"))?;
            let mut hasher = Sha256::new();
            let mut reader = entry.take(unpacked_limit - total + 1);
            let mut buffer = vec![0u8; 64 * 1024];
            loop {
                let read = reader
                    .read(&mut buffer)
                    .map_err(|_| unsafe_archive("a member is corrupt"))?;
                if read == 0 {
                    break;
                }
                total += read as u64;
                // zip =9.0.5 already fails a member that unpacks past its declared
                // size, and the declared total is capped above, so this count
                // is the cap's own guard should the reader ever change.
                if total > unpacked_limit {
                    return Err(unsafe_archive("it unpacks to more than the size cap"));
                }
                hasher.update(&buffer[..read]);
                opened
                    .file
                    .write_all(&buffer[..read])
                    .map_err(|_| unwritable(root, "a file cannot be written"))?;
            }
            opened
                .file
                .sync_all()
                .map_err(|_| unwritable(root, "a file cannot be written"))?;
            #[cfg(unix)]
            if [binary_name(&pin), "cloudflared"].contains(&name.as_str()) {
                use std::os::unix::fs::PermissionsExt;
                opened
                    .file
                    .set_permissions(fs::Permissions::from_mode(0o700))
                    .map_err(|_| unwritable(root, "a file cannot be made executable"))?;
            }
            manifest.insert(name.clone(), format!("{:x}", hasher.finalize()));
        }
        let path = stage.join(MANIFEST);
        let mut opened = crate::board::state::create(&path)
            .map_err(|_| unwritable(root, "its checksums.json cannot be created"))?;
        let bytes = serde_json::to_vec(&manifest).map_err(|e| e.to_string())?;
        opened
            .file
            .write_all(&bytes)
            .and_then(|()| opened.file.sync_all())
            .map_err(|_| unwritable(root, "its checksums.json cannot be written"))?;
        drop(opened);
        let code = probe.help(
            &stage.join(binary_name(&pin)),
            &stage,
            &probe_environment(std::env::vars_os()),
        );
        if code != 0 {
            let outcome = match code {
                PROBE_TIMED_OUT => format!("did not finish within {} s", PROBE_TIMEOUT.as_secs()),
                PROBE_NOT_STARTED => "could not be started".to_owned(),
                code if code > 128 => "was interrupted".to_owned(),
                code => format!("failed (exit {code})"),
            };
            return Err(format!(
                "the downloaded tunnel runtime's check `tunnel-client --help` {outcome}; nothing was installed; check that this host can run {}-{} programs and that no security tool blocks new programs in {}, then retry",
                pin.system,
                pin.arch,
                root.display()
            ));
        }
        // The folder about to be committed must pass the check every rerun
        // applies, or a rerun would refuse what this call reports installed.
        verify(&stage, &pin).map_err(|why| {
            format!(
                "the unpacked tunnel runtime changed while it was checked ({why}); nothing was installed; if this repeats, check whether a security tool adds or alters files in {}, then retry",
                root.display()
            )
        })?;
        let target = root.join(folder_name(&pin));
        if fs::symlink_metadata(&target).is_ok() {
            // Another install finished meanwhile: keep it if it verifies.
            return installed(&target, &pin)?
                .ok_or_else(|| unwritable(root, "its folder vanished"));
        }
        match crate::runtime::rename_retry(&stage, &target) {
            Ok(()) => Ok(Installed {
                binary: target.join(binary_name(&pin)),
                changed: true,
            }),
            // Another install may have won the rename. Otherwise the usual
            // cause is a scan holding the new files a moment; the OS text is
            // not passed on, since it can suggest an elevated prompt.
            Err(_) => installed(&target, &pin)?.ok_or_else(|| {
                format!(
                    "the tunnel runtime could not be moved into {}; nothing was installed; if a security scan is checking the new files, wait a minute, then retry",
                    target.display()
                )
            }),
        }
    })();
    if result.as_ref().is_ok_and(|installed| installed.changed) {
        return result;
    }
    // The stage is this call's own fresh folder: remove the files in it,
    // whatever put them there, then the folder; a leftover is named. It is
    // listed only while it is still a plain folder, never through a link.
    let plain = fs::symlink_metadata(&stage).is_ok_and(|info| info.is_dir());
    if let Some(entries) = plain.then(|| fs::read_dir(&stage).ok()).flatten() {
        for entry in entries.flatten() {
            if entry
                .file_type()
                .is_ok_and(|kind| kind.is_file() || kind.is_symlink())
            {
                remove_retry(|| fs::remove_file(entry.path()));
            }
        }
    }
    remove_retry(|| fs::remove_dir(&stage));
    match result {
        Err(error) if fs::symlink_metadata(&stage).is_ok() => Err(format!(
            "{error} (the partial folder {} could not be removed; delete it once nothing holds it open)",
            stage.display()
        )),
        other => other,
    }
}

/// The archive's members in order, after every check that needs no
/// decompression: count, header agreement, names, types, encryption,
/// compression, declared sizes and the wrapper's presence.
fn members(
    zip: &mut zip::ZipArchive<Cursor<&[u8]>>,
    pin: &Pin,
    unpacked_limit: u64,
) -> Result<Vec<String>, String> {
    if zip.is_empty() || zip.len() > MEMBER_LIMIT {
        return Err(unsafe_archive("it holds no members, or more than expected"));
    }
    // Refuses duplicate names, which the reader would otherwise collapse,
    // and local headers that disagree with the central directory.
    zip.validate_header_consistency()
        .map_err(|_| unsafe_archive("its headers disagree or repeat a name"))?;
    let mut names = Vec::new();
    let mut folded = BTreeSet::new();
    let mut declared = 0u64;
    for index in 0..zip.len() {
        let entry = zip
            .by_index_data(index)
            .map_err(|_| unsafe_archive("a member cannot be read"))?;
        let name = entry
            .name()
            .map_err(|_| unsafe_archive("a member name is not text"))?
            .into_owned();
        if let Some(why) = member_problem(&name) {
            return Err(unsafe_member(&name, why));
        }
        if name.eq_ignore_ascii_case(MANIFEST) {
            return Err(unsafe_member(
                &name,
                "it would replace the manifest this client writes",
            ));
        }
        if !folded.insert(name.to_ascii_lowercase()) {
            return Err(unsafe_member(&name, "another member has the same name"));
        }
        // No type bits (a DOS-made entry) or a regular file; never a link,
        // directory, device, FIFO or socket.
        let kind = entry.unix_mode().map_or(0, |mode| mode & 0o170000);
        if entry.is_dir() || (kind != 0 && kind != 0o100000) {
            return Err(unsafe_member(&name, "it is not a regular file"));
        }
        if entry.encrypted()
            || !matches!(
                entry.compression(),
                zip::CompressionMethod::Stored | zip::CompressionMethod::Deflated
            )
        {
            return Err(unsafe_member(
                &name,
                "it is encrypted or compressed in an unexpected way",
            ));
        }
        declared = declared.saturating_add(entry.size());
        if declared > unpacked_limit {
            return Err(unsafe_archive("its members declare more than the size cap"));
        }
        names.push(name);
    }
    if !names.iter().any(|name| name == binary_name(pin)) {
        return Err(unsafe_archive(
            "it does not contain the tunnel-client program",
        ));
    }
    Ok(names)
}

fn unsafe_archive(why: &str) -> String {
    format!(
        "the tunnel runtime archive was refused: {why}; nothing was installed; the pinned release never does this, so report it"
    )
}

fn unsafe_member(name: &str, why: &str) -> String {
    unsafe_archive(&format!("its member {name:?} is unsafe ({why})"))
}

fn unwritable(root: &Path, why: &str) -> String {
    format!(
        "the tunnel runtime could not be installed under {}: {why}; nothing was installed; free some disk space or repair that folder's permissions, then retry",
        root.display()
    )
}

/// Windows can hold a just-written or just-run program open for a moment
/// (an antivirus scan); a removal it refuses then is retried briefly.
fn remove_retry(remove: impl Fn() -> io::Result<()>) {
    for attempt in 0..5u64 {
        match remove() {
            Err(error) if error.kind() == io::ErrorKind::PermissionDenied && attempt < 4 => {
                std::thread::sleep(Duration::from_millis(100 * (attempt + 1)))
            }
            _ => return,
        }
    }
}

/// Download `url` over HTTPS only (redirects included), refusing more than
/// `limit` bytes. No credentials are sent; the caller checks the size and
/// digest of whatever comes back.
fn download(url: &str, limit: u64) -> Result<Vec<u8>, String> {
    use futures::StreamExt;
    /// Why a download stopped, by where the next step lies.
    enum Stop {
        Network(String),
        TooLarge,
        Local(&'static str),
    }
    let owned = url.to_owned();
    // A private runtime avoids nesting Tokio runtimes in synchronous callers
    // (as `runtime::install_source` does).
    let outcome = std::thread::spawn(move || -> Result<Vec<u8>, Stop> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| Stop::Local("no I/O runtime could be started"))?;
        runtime.block_on(async move {
            let cause = |error: reqwest::Error| {
                Stop::Network(if error.is_timeout() {
                    "the server stopped answering in time".to_owned()
                } else if let Some(status) = error.status() {
                    format!("the server answered HTTP {}", status.as_u16())
                } else if error.is_connect() {
                    "no connection could be made (a proxy or security product that inspects HTTPS must be trusted by this system)".to_owned()
                } else {
                    "the transfer failed".to_owned()
                })
            };
            let client = reqwest::Client::builder()
                .https_only(true)
                .user_agent("pseudolife-mcp")
                .connect_timeout(CONNECT_TIMEOUT)
                .read_timeout(READ_TIMEOUT)
                .timeout(TRANSFER_TIMEOUT)
                .build()
                .map_err(|_| Stop::Local("no HTTPS client could be built"))?;
            let response = client
                .get(&owned)
                .send()
                .await
                .and_then(reqwest::Response::error_for_status)
                .map_err(cause)?;
            if response.content_length().is_some_and(|n| n > limit) {
                return Err(Stop::TooLarge);
            }
            let mut stream = response.bytes_stream();
            let mut bytes = Vec::new();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(cause)?;
                if bytes.len() as u64 + chunk.len() as u64 > limit {
                    return Err(Stop::TooLarge);
                }
                bytes.extend_from_slice(&chunk);
            }
            Ok(bytes)
        })
    })
    .join()
    .unwrap_or(Err(Stop::Local("the download worker failed")));
    outcome.map_err(|stop| match stop {
        Stop::Network(why) => format!(
            "could not download the tunnel runtime from {url}: {why}; check that this host can reach github.com over HTTPS, then retry"
        ),
        Stop::TooLarge => "the tunnel runtime download is larger than the pinned release; nothing was installed; if the host reaches the internet through a proxy or a sign-in portal, check that it passes downloads from github.com unchanged, then retry".to_owned(),
        Stop::Local(why) => format!(
            "the tunnel runtime download could not start ({why}); nothing was installed; retry, and if it repeats, report it"
        ),
    })
}

#[cfg(test)]
#[path = "vendor_tests.rs"]
mod tests;
