//! In-process tests of the vendor runtime: archives are built in memory and
//! injected, the download is a closure and the `--help` probe a fake
//! `Probe`, so nothing here touches the network or starts a process.
use std::{cell::RefCell, io::Write as _};

use zip::write::SimpleFileOptions;

use super::*;

/// A disposable owner-only tunnel store.
struct Store {
    base: PathBuf,
    root: PathBuf,
}
impl Store {
    fn new() -> Self {
        let base = std::env::temp_dir().join(format!("tunnel-vendor-{}", uuid::Uuid::new_v4()));
        let root = base.join("tunnel");
        crate::board::state::private_dir(&root).unwrap();
        Self { base, root }
    }
    /// Every name under the root, recursively, for "nothing was left" checks.
    fn listing(&self) -> Vec<String> {
        fn walk(dir: &Path, prefix: &str, out: &mut Vec<String>) {
            for entry in fs::read_dir(dir).unwrap() {
                let entry = entry.unwrap();
                let name = format!("{prefix}{}", entry.file_name().to_string_lossy());
                if entry.file_type().unwrap().is_dir() {
                    walk(&entry.path(), &format!("{name}/"), out);
                }
                out.push(name);
            }
        }
        let mut out = Vec::new();
        walk(&self.root, "", &mut out);
        out.sort();
        out
    }
}
impl Drop for Store {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.base);
    }
}

/// One `--help` probe as the install asked for it.
struct Call {
    program: PathBuf,
    cwd: PathBuf,
    env: Vec<(OsString, OsString)>,
}

/// The `--help` probe: answers `code`, records each call and checks the
/// program it is asked to run exists at that moment.
struct Fake {
    code: i32,
    calls: RefCell<Vec<Call>>,
    during: Box<dyn Fn(&Path)>,
}
impl Fake {
    fn new(code: i32) -> Self {
        Self {
            code,
            calls: RefCell::new(Vec::new()),
            during: Box::new(|_| {}),
        }
    }
}
impl Probe for Fake {
    fn help(&self, program: &Path, cwd: &Path, env: &[(OsString, OsString)]) -> i32 {
        assert!(program.is_file(), "probe of a missing {program:?}");
        (self.during)(program);
        self.calls.borrow_mut().push(Call {
            program: program.to_owned(),
            cwd: cwd.to_owned(),
            env: env.to_vec(),
        });
        self.code
    }
}

/// This host's pin with the size and digest of `archive`.
fn pin_of(archive: &[u8]) -> Pin<'static> {
    let host = pin_for(std::env::consts::OS, std::env::consts::ARCH).unwrap();
    Pin {
        size: archive.len() as u64,
        sha256: Box::leak(format!("{:x}", Sha256::digest(archive)).into_boxed_str()),
        ..host
    }
}

const REGULAR: u32 = 0o100644;

/// A zip of `(name, bytes, unix mode)` members, deflated and Unix-made, the
/// way the vendor builds them on every host (the writer's default on
/// Windows is DOS-made, whose modes the reader synthesises instead).
fn zip_of(members: &[(&str, &[u8], u32)]) -> Vec<u8> {
    let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, data, mode) in members {
        let options = SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated)
            .system(zip::System::Unix)
            .external_attributes(mode << 16);
        writer.start_file(*name, options).unwrap();
        writer.write_all(data).unwrap();
    }
    writer.finish().unwrap().into_inner()
}

/// The real release's member list, with small stand-in bodies.
fn release(pin: &Pin) -> Vec<u8> {
    let wrapper = binary_name(pin);
    let cloudflared = if pin.system == "windows" {
        "cloudflared.exe"
    } else {
        "cloudflared"
    };
    zip_of(&[
        (wrapper, b"wrapper program", 0o100755),
        (cloudflared, b"tunnel program", 0o100755),
        ("cloudflared-manifest.json", b"{}", REGULAR),
        ("LICENSE", b"license", REGULAR),
        ("NOTICE", b"notice", REGULAR),
        ("tunnel-client-v0.0.15-licenses.txt", b"licenses", 0o100600),
        ("tunnel-client-v0.0.15.spdx.json", b"{}", 0o100600),
    ])
}

fn host() -> Pin<'static> {
    pin_for(std::env::consts::OS, std::env::consts::ARCH).unwrap()
}

/// Install `archive` against a pin made from `pinned`'s bytes.
fn attempt(
    store: &Store,
    pinned: &[u8],
    archive: &[u8],
    probe: &Fake,
) -> Result<Installed, String> {
    install(&store.root, pin_of(pinned), archive, probe, UNPACKED_LIMIT)
}

/// The archive is refused, the probe never ran and the store is as before.
fn refused(archive: &[u8], fragment: &str) {
    let store = Store::new();
    let before = store.listing();
    let probe = Fake::new(0);
    let error = attempt(&store, archive, archive, &probe).unwrap_err();
    assert!(error.contains(fragment), "{fragment:?} not in {error:?}");
    assert!(error.contains("nothing was installed"), "{error}");
    assert!(probe.calls.borrow().is_empty());
    assert_eq!(store.listing(), before, "{error}");
}

#[test]
fn a_pinned_archive_installs_flat_owner_only_and_probed() {
    let store = Store::new();
    let archive = release(&host());
    let probe = Fake::new(0);
    let fetched = RefCell::new(Vec::new());
    let installed = ensure_with(&store.root, pin_of(&archive), &probe, |url, size| {
        fetched.borrow_mut().push((url.to_owned(), size));
        Ok(archive.clone())
    })
    .unwrap();
    let folder = store.root.join(folder_name(&host()));
    assert!(installed.changed);
    assert_eq!(installed.binary, folder.join(binary_name(&host())));
    assert!(
        installed
            .summary()
            .contains("Installed the tunnel runtime 0.0.15")
    );
    assert_eq!(
        *fetched.borrow(),
        [(url(&host()), archive.len() as u64)],
        "one download, of the pinned asset, capped at its exact size"
    );
    assert!(fetched.borrow()[0].0.starts_with(
        "https://github.com/openai/tunnel-client/releases/download/v0.0.15/tunnel-client-v0.0.15-"
    ));
    // One probe, of the staged wrapper, run in the stage with only the
    // allowlisted environment.
    let calls = probe.calls.borrow();
    assert_eq!(calls.len(), 1);
    let staged = calls[0].program.parent().unwrap();
    assert_eq!(staged.parent().unwrap(), store.root);
    assert_eq!(calls[0].cwd, staged);
    assert_eq!(calls[0].env, probe_environment(std::env::vars_os()));
    for (key, _) in &calls[0].env {
        let key = key.to_str().unwrap().to_ascii_uppercase();
        assert!(
            PROBE_ENVIRONMENT.contains(&key.as_str()),
            "{key} reached the probe"
        );
    }
    assert!(
        staged
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with(".runtime-")
    );
    // Flat: the members and the manifest, nothing else, no stage left.
    assert_eq!(store.listing().len(), 1 + 8, "{:?}", store.listing());
    let manifest: BTreeMap<String, String> =
        serde_json::from_slice(&crate::board::state::read(&folder.join(MANIFEST), 65536).unwrap())
            .unwrap();
    assert_eq!(manifest.len(), 7);
    assert_eq!(
        manifest[binary_name(&host())],
        format!("{:x}", Sha256::digest(b"wrapper program"))
    );
    for name in manifest.keys() {
        // Each file passes the client's owner-only open.
        crate::board::state::open(&folder.join(name)).unwrap();
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = |name: &str| {
            fs::metadata(folder.join(name))
                .unwrap()
                .permissions()
                .mode()
                & 0o777
        };
        assert_eq!(mode("tunnel-client"), 0o700);
        assert_eq!(mode("cloudflared"), 0o700);
        // The archive's own 0644 is not copied.
        assert_eq!(mode("LICENSE"), 0o600);
    }
}

#[test]
fn a_rerun_reuses_the_verified_runtime_and_changes_nothing() {
    let store = Store::new();
    let archive = release(&host());
    let pin = pin_of(&archive);
    ensure_with(&store.root, pin, &Fake::new(0), |_, _| Ok(archive.clone())).unwrap();
    let before = store.listing();
    let folder = store.root.join(folder_name(&pin));
    let manifest = fs::read(folder.join(MANIFEST)).unwrap();
    let probe = Fake::new(0);
    let again = ensure_with(
        &store.root,
        pin,
        &probe,
        |_, _| -> Result<Vec<u8>, String> { panic!("a verified runtime is downloaded again") },
    )
    .unwrap();
    assert!(!again.changed);
    assert_eq!(again.binary, folder.join(binary_name(&pin)));
    assert!(
        again
            .summary()
            .contains("already installed and verified; nothing changed")
    );
    assert!(probe.calls.borrow().is_empty());
    assert_eq!(store.listing(), before);
    assert_eq!(fs::read(folder.join(MANIFEST)).unwrap(), manifest);
}

#[test]
fn a_later_spawn_refuses_a_tampered_vendor_image() {
    let store = Store::new();
    let archive = release(&host());
    let pin = pin_of(&archive);
    let installed = ensure_with(&store.root, pin, &Fake::new(0), |_, _| Ok(archive)).unwrap();
    assert!(reverify(&store.root, &installed.binary).is_ok());
    fs::write(&installed.binary, b"changed wrapper fixture").unwrap();
    assert!(
        reverify(&store.root, &installed.binary)
            .unwrap_err()
            .contains("failed its integrity check")
    );
}

#[test]
fn a_folder_in_the_python_clients_layout_is_reused() {
    let store = Store::new();
    let pin = host();
    let folder = store.root.join(folder_name(&pin));
    crate::board::state::private_dir(&folder).unwrap();
    let mut manifest = String::from("{");
    for (index, (name, data)) in [(binary_name(&pin), &b"wrapper"[..]), ("LICENSE", b"text")]
        .into_iter()
        .enumerate()
    {
        crate::board::state::atomic_create(&folder.join(name), data).unwrap();
        // Python's chmod of the wrapper.
        #[cfg(unix)]
        if index == 0 {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(folder.join(name), fs::Permissions::from_mode(0o700)).unwrap();
        }
        let comma = if index == 0 { "" } else { ", " };
        // json.dumps's spacing.
        manifest += &format!("{comma}\"{name}\": \"{:x}\"", Sha256::digest(data));
    }
    manifest += "}";
    crate::board::state::atomic_create(&folder.join(MANIFEST), manifest.as_bytes()).unwrap();
    let installed = ensure_with(
        &store.root,
        pin,
        &Fake::new(0),
        |_, _| -> Result<Vec<u8>, String> { panic!("downloaded") },
    )
    .unwrap();
    assert!(!installed.changed);
}

#[test]
fn a_wrong_digest_is_refused_and_leaves_nothing() {
    let store = Store::new();
    let archive = release(&host());
    let mut other = archive.clone();
    let last = other.len() - 1;
    other[last] ^= 1;
    let probe = Fake::new(0);
    let error = attempt(&store, &archive, &other, &probe).unwrap_err();
    assert!(
        error.contains("does not match the checksum this client pins"),
        "{error}"
    );
    assert!(error.contains(&format!("expected {:x}", Sha256::digest(&archive))));
    assert!(store.listing().is_empty());
    assert!(probe.calls.borrow().is_empty());
}

#[test]
fn a_short_or_oversized_download_is_refused_and_leaves_nothing() {
    let store = Store::new();
    let archive = release(&host());
    let short = &archive[..archive.len() - 1];
    let error = attempt(&store, &archive, short, &Fake::new(0)).unwrap_err();
    assert!(
        error.contains(&format!(
            "ended early ({} of {} bytes)",
            short.len(),
            archive.len()
        )),
        "{error}"
    );
    let mut long = archive.clone();
    long.push(0);
    let error = attempt(&store, &archive, &long, &Fake::new(0)).unwrap_err();
    assert!(error.contains("larger than the pinned release"), "{error}");
    assert!(store.listing().is_empty());
}

#[test]
fn a_fetch_failure_is_returned_and_leaves_nothing() {
    let store = Store::new();
    let pin = host();
    let error = ensure_with(&store.root, pin, &Fake::new(0), |_, _| {
        Err("offline".to_owned())
    })
    .unwrap_err();
    assert_eq!(error, "offline");
    assert!(store.listing().is_empty());
}

#[test]
fn a_link_member_is_refused() {
    let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
    let options = SimpleFileOptions::default();
    writer.start_file(binary_name(&host()), options).unwrap();
    writer.write_all(b"wrapper").unwrap();
    writer
        .add_symlink("LICENSE", "/etc/passwd", options)
        .unwrap();
    refused(
        &writer.finish().unwrap().into_inner(),
        "\"LICENSE\" is unsafe (it is not a regular file)",
    );
}

#[test]
fn unsafe_member_names_are_refused() {
    let wrapper = binary_name(&host());
    for (name, why) in [
        ("bin/tool", "it is in a folder"),
        ("..\\tool", "it is in a folder"),
        ("/etc/tool", "it is in a folder"),
        ("../tool", "it is in a folder"),
        ("C:tool", "it names a drive"),
        (
            "tool:stream",
            "it names a drive or an alternate data stream",
        ),
        ("..", "it is hidden or a parent reference"),
        (".profile", "it is hidden"),
        ("tool name", "its name has characters outside"),
        ("tóol", "its name has characters outside"),
        ("NUL.txt", "it is a Windows device name"),
        ("com1", "it is a Windows device name"),
        ("tool.", "Windows would drop its trailing dot"),
        ("CHECKSUMS.JSON", "it would replace the manifest"),
    ] {
        let archive = zip_of(&[(wrapper, b"wrapper", REGULAR), (name, b"x", REGULAR)]);
        refused(&archive, &format!("{name:?} is unsafe ({why}"));
    }
    // A name equal to another but for case (one file on Windows and macOS).
    let shouted = wrapper.to_ascii_uppercase();
    let archive = zip_of(&[(wrapper, b"wrapper", REGULAR), (&shouted, b"x", REGULAR)]);
    refused(
        &archive,
        &format!("{shouted:?} is unsafe (another member has the same name)"),
    );
}

#[test]
fn an_exactly_repeated_name_is_refused() {
    // The writer refuses a repeat, so a second member's name is patched
    // into the first's in both its local and central headers.
    let wrapper = binary_name(&host());
    let decoy = "x".repeat(wrapper.len());
    let mut archive = zip_of(&[(wrapper, b"wrapper", REGULAR), (&decoy, b"other", REGULAR)]);
    let mut patched = 0;
    let mut at = 0;
    while let Some(found) = archive[at..]
        .windows(decoy.len())
        .position(|w| w == decoy.as_bytes())
    {
        let start = at + found;
        archive[start..start + decoy.len()].copy_from_slice(wrapper.as_bytes());
        patched += 1;
        at = start + decoy.len();
    }
    assert_eq!(patched, 2, "the local and central names");
    refused(&archive, "repeat a name");
}

#[test]
fn directories_and_special_files_are_refused() {
    let wrapper = binary_name(&host());
    let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
    writer
        .start_file(wrapper, SimpleFileOptions::default())
        .unwrap();
    writer
        .add_directory("docs", SimpleFileOptions::default())
        .unwrap();
    refused(
        &writer.finish().unwrap().into_inner(),
        "\"docs/\" is unsafe (it is in a folder",
    );
    for (mode, kind) in [
        (0o010644, "fifo"),
        (0o020644, "character device"),
        (0o060644, "block device"),
        (0o140644, "socket"),
        (0o040755, "directory"),
    ] {
        let archive = zip_of(&[(wrapper, b"wrapper", REGULAR), ("special", b"x", mode)]);
        refused(&archive, "\"special\" is unsafe (it is not a regular file)");
        let _ = kind;
    }
}

#[test]
fn an_archive_without_the_wrapper_or_with_too_many_members_is_refused() {
    refused(
        &zip_of(&[("LICENSE", b"text", REGULAR)]),
        "does not contain the tunnel-client program",
    );
    let names: Vec<String> = (0..=MEMBER_LIMIT).map(|i| format!("file{i}")).collect();
    let mut members: Vec<(&str, &[u8], u32)> = names
        .iter()
        .map(|n| (n.as_str(), &b"x"[..], REGULAR))
        .collect();
    members[0].0 = binary_name(&host());
    refused(&zip_of(&members), "more than expected");
    let empty = zip::ZipWriter::new(Cursor::new(Vec::new()))
        .finish()
        .unwrap()
        .into_inner();
    refused(&empty, "no members");
    refused(b"not a zip at all", "not a readable zip");
}

#[test]
fn the_unpacked_total_is_capped_and_a_member_past_its_declared_size_refused() {
    let store = Store::new();
    let wrapper = binary_name(&host());
    let archive = zip_of(&[
        (wrapper, &[7u8; 4096], REGULAR),
        ("LICENSE", &[0u8; 4096], REGULAR),
    ]);
    let pin = pin_of(&archive);
    // Declared sizes over the cap: refused before a stage exists.
    let error = install(&store.root, pin, &archive, &Fake::new(0), 4096).unwrap_err();
    assert!(
        error.contains("its members declare more than the size cap"),
        "{error}"
    );
    assert!(store.listing().is_empty());
    // A member that unpacks past what it declares: patch the second
    // member's declared size (central directory) down to 1, so only the
    // counted bytes can catch it.
    let mut lying = archive.clone();
    let central = lying.windows(4).rposition(|w| w == b"PK\x01\x02").unwrap();
    lying[central + 24..central + 28].copy_from_slice(&1u32.to_le_bytes());
    let local = lying.windows(4).rposition(|w| w == b"PK\x03\x04").unwrap();
    lying[local + 22..local + 26].copy_from_slice(&1u32.to_le_bytes());
    let pin = pin_of(&lying);
    let error = install(&store.root, pin, &lying, &Fake::new(0), 4096 + 2).unwrap_err();
    // The reader stops at the declared size, before the counted cap.
    assert!(error.contains("a member is corrupt"), "{error}");
    assert!(store.listing().is_empty(), "{:?}", store.listing());
}

#[test]
fn a_failed_probe_is_refused_and_leaves_nothing() {
    let store = Store::new();
    let archive = release(&host());
    let probe = Fake::new(3);
    let error = attempt(&store, &archive, &archive, &probe).unwrap_err();
    assert!(
        error.contains("check `tunnel-client --help` failed (exit 3)"),
        "{error}"
    );
    assert_eq!(probe.calls.borrow().len(), 1);
    assert!(store.listing().is_empty(), "{:?}", store.listing());
}

#[test]
fn the_probe_environment_is_exactly_the_allowlist() {
    let pair = |key: &str, value: &str| (OsString::from(key), OsString::from(value));
    let given = vec![
        pair("PATH", "/usr/bin"),
        pair("HOME", "/home/someone"),
        pair("SSL_CERT_FILE", "/etc/ssl/cert.pem"),
        pair("PSEUDOLIFE_MCP_TOKEN", "secret-token"),
        pair("PSEUDOLIFE_MCP_TOKEN_FILE", "/home/someone/token"),
        pair("OPENAI_API_KEY", "sk-secret"),
        pair("AWS_SECRET_ACCESS_KEY", "secret"),
        pair("PATHX", "not PATH"),
        pair("LD_PRELOAD", "/tmp/evil.so"),
    ];
    let kept = probe_environment(given);
    assert_eq!(
        kept,
        vec![
            pair("PATH", "/usr/bin"),
            pair("HOME", "/home/someone"),
            pair("SSL_CERT_FILE", "/etc/ssl/cert.pem"),
        ]
    );
    // Windows ignores the case of names; elsewhere a lower-case name is
    // another variable.
    let mixed = probe_environment(vec![
        pair("Path", "C:\\Windows"),
        pair("SystemRoot", "C:\\Windows"),
    ]);
    assert_eq!(mixed.len(), if cfg!(windows) { 2 } else { 0 });
}

#[test]
fn a_probe_that_cannot_start_is_named() {
    let store = Store::new();
    let archive = release(&host());
    let error = attempt(&store, &archive, &archive, &Fake::new(PROBE_NOT_STARTED)).unwrap_err();
    assert!(error.contains("--help` could not be started"), "{error}");
    assert!(store.listing().is_empty(), "{:?}", store.listing());
}

#[test]
fn a_probe_timeout_or_interrupt_is_named() {
    for (code, says) in [
        (124, "did not finish within 15 s"),
        (130, "was interrupted"),
    ] {
        let store = Store::new();
        let archive = release(&host());
        let error = attempt(&store, &archive, &archive, &Fake::new(code)).unwrap_err();
        assert!(error.contains(says), "{error}");
        assert!(store.listing().is_empty(), "{:?}", store.listing());
    }
}

#[test]
fn a_stage_replaced_by_a_link_is_not_emptied_through_it() {
    // The failure path must not list its stage through a link: here the
    // stage is moved aside during a failing probe and a link put in its
    // place, so a cleanup that followed it would empty the moved folder.
    let store = Store::new();
    let archive = release(&host());
    let aside = store.base.join("moved-stage");
    let mut probe = Fake::new(3);
    let moved = aside.clone();
    probe.during = Box::new(move |wrapper| {
        let stage = wrapper.parent().unwrap();
        fs::rename(stage, &moved).unwrap();
        dir_link(&moved, stage);
    });
    let error = attempt(&store, &archive, &archive, &probe).unwrap_err();
    assert!(error.contains("failed (exit 3)"), "{error}");
    let wrapper = aside.join(binary_name(&host()));
    assert_eq!(fs::read(&wrapper).unwrap(), b"wrapper program");
    assert!(aside.join("LICENSE").exists() && aside.join(MANIFEST).exists());
}

#[test]
fn a_stage_that_changes_during_the_probe_is_refused_and_removed() {
    // Something (a scan, the program itself) adds a file beside the wrapper:
    // a rerun would refuse that folder, so this call must not commit it.
    let store = Store::new();
    let archive = release(&host());
    let mut probe = Fake::new(0);
    probe.during = Box::new(|wrapper| {
        fs::write(wrapper.with_file_name("scan.log"), b"added").unwrap();
    });
    let error = attempt(&store, &archive, &archive, &probe).unwrap_err();
    assert!(error.contains("changed while it was checked"), "{error}");
    assert!(error.contains("does not name"), "{error}");
    assert!(store.listing().is_empty(), "{:?}", store.listing());
}

/// Set the modification time of `path` itself (a folder, or a link or
/// junction, never what it points at) `by` into the past.
fn age(path: &Path, by: Duration) {
    let when = std::time::SystemTime::now() - by;
    #[cfg(unix)]
    {
        let since = when.duration_since(std::time::UNIX_EPOCH).unwrap();
        let stamp = rustix::fs::Timespec {
            tv_sec: since.as_secs() as _,
            tv_nsec: since.subsec_nanos() as _,
        };
        rustix::fs::utimensat(
            rustix::fs::CWD,
            path,
            &rustix::fs::Timestamps {
                last_access: stamp,
                last_modification: stamp,
            },
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        )
        .unwrap();
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // FILE_WRITE_ATTRIBUTES on the entry itself (BACKUP_SEMANTICS for a
        // folder, OPEN_REPARSE_POINT so a junction is not followed).
        fs::OpenOptions::new()
            .access_mode(0x0100)
            .custom_flags(0x0200_0000 | 0x0020_0000)
            .open(path)
            .unwrap()
            .set_modified(when)
            .unwrap();
    }
}

#[test]
fn an_install_removes_stale_stages_and_leaves_anything_else() {
    let store = Store::new();
    let stage = |name: &str, files: &[&str], old: bool| {
        let path = store.root.join(name);
        crate::board::state::private_dir(&path).unwrap();
        for file in files {
            crate::board::state::atomic_create(&path.join(file), b"x").unwrap();
        }
        if old {
            age(&path, Duration::from_secs(2 * 3600));
        }
        path
    };
    let ours = stage(
        ".runtime-0123456789abcdef0123456789abcdef",
        &["LICENSE", MANIFEST],
        true,
    );
    let python = stage(".runtime-ab_12cd3", &[], true);
    let fresh = stage(
        ".runtime-fedcba9876543210fedcba9876543210",
        &["LICENSE"],
        false,
    );
    let odd = stage(".runtime-0000aaaa", &["LICENSE"], true);
    crate::board::state::private_dir(&odd.join("nested")).unwrap();
    age(&odd, Duration::from_secs(2 * 3600));
    let foreign = stage(".runtime-old", &["notes.txt", "x y"], true);
    // Near misses: plausible file names, but folder names no installer
    // generates (a word, nine characters, a 32-character non-hex suffix).
    let backup = stage(".runtime-backup", &["notes.txt"], true);
    let nine = stage(".runtime-abcd12345", &["LICENSE"], true);
    let not_hex = stage(
        ".runtime-0123456789abcdef0123456789abcdeg",
        &["LICENSE"],
        true,
    );
    let other = stage("keep-me", &["LICENSE"], true);
    let archive = release(&host());
    ensure_with(&store.root, pin_of(&archive), &Fake::new(0), |_, _| {
        Ok(archive.clone())
    })
    .unwrap();
    assert!(!ours.exists() && !python.exists(), "{:?}", store.listing());
    for kept in [&fresh, &odd, &foreign, &backup, &nine, &not_hex, &other] {
        assert!(kept.exists(), "{kept:?} removed: {:?}", store.listing());
    }
}

#[test]
fn a_stale_stage_beside_an_installed_runtime_is_removed_on_reuse() {
    let store = Store::new();
    let archive = release(&host());
    let pin = pin_of(&archive);
    install(&store.root, pin, &archive, &Fake::new(0), UNPACKED_LIMIT).unwrap();
    // An install interrupted in its probe left this stage behind.
    let left = store.root.join(".runtime-00112233445566778899aabbccddeeff");
    crate::board::state::private_dir(&left).unwrap();
    crate::board::state::atomic_create(&left.join(binary_name(&pin)), b"partial").unwrap();
    age(&left, Duration::from_secs(2 * 3600));
    let again = ensure_with(
        &store.root,
        pin,
        &Fake::new(0),
        |_, _| -> Result<Vec<u8>, String> { panic!("a verified runtime is downloaded again") },
    )
    .unwrap();
    assert!(!again.changed);
    assert!(!left.exists(), "{:?}", store.listing());
}

/// An aged directory link named like a stage, pointing at `target`.
fn stage_link(store: &Store, name: &str, target: &Path) -> PathBuf {
    let link = store.root.join(name);
    dir_link(target, &link);
    age(&link, Duration::from_secs(2 * 3600));
    link
}

#[test]
fn the_sweep_never_follows_a_link_or_takes_a_folder_others_can_use() {
    // The links here are this user's own, in an owner-only folder: ones the
    // private-path rule would follow above a private file. The sweep follows
    // none.
    let store = Store::new();
    // A folder outside the store holding names an install writes: what a
    // sweep following a link would empty.
    let victim = store.base.join("victim");
    crate::board::state::private_dir(&victim).unwrap();
    for name in ["LICENSE", "NOTICE", MANIFEST] {
        crate::board::state::atomic_create(&victim.join(name), b"keep").unwrap();
    }
    // A junction (Windows) or symlink (POSIX) named exactly like a stage.
    let link = stage_link(&store, ".runtime-ffeeddccbbaa99887766554433221100", &victim);
    // A real, aged stage holding a link to that folder.
    let holder = store.root.join(".runtime-aabbccddeeff00112233445566778899");
    crate::board::state::private_dir(&holder).unwrap();
    crate::board::state::atomic_create(&holder.join("LICENSE"), b"x").unwrap();
    let inner = holder.join("NOTICE");
    #[cfg(unix)]
    std::os::unix::fs::symlink(victim.join("NOTICE"), &inner).unwrap();
    #[cfg(windows)]
    dir_link(&victim, &inner);
    age(&holder, Duration::from_secs(2 * 3600));
    // An aged stage others can read and enter (POSIX modes).
    #[cfg(unix)]
    let shared = {
        use std::os::unix::fs::PermissionsExt;
        let shared = store.root.join(".runtime-0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f");
        crate::board::state::private_dir(&shared).unwrap();
        crate::board::state::atomic_create(&shared.join("LICENSE"), b"x").unwrap();
        fs::set_permissions(&shared, fs::Permissions::from_mode(0o755)).unwrap();
        age(&shared, Duration::from_secs(2 * 3600));
        shared
    };
    let archive = release(&host());
    ensure_with(&store.root, pin_of(&archive), &Fake::new(0), |_, _| {
        Ok(archive.clone())
    })
    .unwrap();
    for name in ["LICENSE", "NOTICE", MANIFEST] {
        assert_eq!(
            fs::read(victim.join(name)).unwrap(),
            b"keep",
            "{name} under the link"
        );
    }
    assert!(fs::symlink_metadata(&link).is_ok(), "the link was removed");
    assert!(
        fs::symlink_metadata(&inner).is_ok(),
        "the link inside a stage was removed"
    );
    assert!(
        holder.join("LICENSE").exists(),
        "a stage holding a link was swept"
    );
    #[cfg(unix)]
    assert!(
        shared.join("LICENSE").exists(),
        "a stage others can use was swept"
    );
}

#[test]
fn an_install_that_finished_meanwhile_is_kept_and_the_stage_removed() {
    let store = Store::new();
    let archive = release(&host());
    let pin = pin_of(&archive);
    // A first install, moved aside to stand in for a concurrent one.
    install(&store.root, pin, &archive, &Fake::new(0), UNPACKED_LIMIT).unwrap();
    let folder = store.root.join(folder_name(&pin));
    let aside = store.root.join("aside");
    fs::rename(&folder, &aside).unwrap();
    let mut probe = Fake::new(0);
    let (from, to) = (aside.clone(), folder.clone());
    probe.during = Box::new(move |_| fs::rename(&from, &to).unwrap());
    let installed = install(&store.root, pin, &archive, &probe, UNPACKED_LIMIT).unwrap();
    assert!(!installed.changed);
    assert!(
        !store
            .listing()
            .iter()
            .any(|name| name.starts_with(".runtime-")),
        "{:?}",
        store.listing()
    );
}

#[test]
fn the_platform_mapping_covers_the_six_pins_and_names_any_other() {
    let rust = [
        ("macos", "x86_64"),
        ("macos", "aarch64"),
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("windows", "x86_64"),
        ("windows", "aarch64"),
    ];
    for ((os, arch), pin) in rust.into_iter().zip(PINS) {
        let found = pin_for(os, arch).unwrap();
        assert_eq!(
            (found.system, found.arch, found.sha256),
            (pin.system, pin.arch, pin.sha256)
        );
        assert!(is_digest(pin.sha256) && pin.size > 20_000_000 && pin.size < 30_000_000);
    }
    let digests: BTreeSet<_> = PINS.iter().map(|pin| pin.sha256).collect();
    assert_eq!(digests.len(), 6);
    for (os, arch) in [
        ("freebsd", "x86_64"),
        ("linux", "riscv64"),
        ("windows", "x86"),
    ] {
        let error = pin_for(os, arch).unwrap_err();
        assert!(
            error.contains(&format!("this platform ({os}-{arch})")),
            "{error}"
        );
    }
    assert_eq!(binary_name(&PINS[4]), "tunnel-client.exe");
    assert_eq!(binary_name(&PINS[2]), "tunnel-client");
}

/// The installed runtime, then a broken copy of it: each break refuses
/// with the integrity message, names the folder and changes nothing.
fn broken_install_is_refused(fragment: &str, break_it: impl FnOnce(&Path, &Pin)) {
    let store = Store::new();
    let archive = release(&host());
    let pin = pin_of(&archive);
    install(&store.root, pin, &archive, &Fake::new(0), UNPACKED_LIMIT).unwrap();
    let folder = store.root.join(folder_name(&pin));
    break_it(&folder, &pin);
    let before = store.listing();
    let error = ensure_with(
        &store.root,
        pin,
        &Fake::new(0),
        |_, _| -> Result<Vec<u8>, String> { panic!("a broken runtime is downloaded over") },
    )
    .unwrap_err();
    assert!(error.contains("failed its integrity check"), "{error}");
    assert!(error.contains(fragment), "{fragment:?} not in {error}");
    assert!(error.contains("move that folder aside"), "{error}");
    assert_eq!(store.listing(), before);
}

#[test]
fn a_tampered_installed_file_is_refused() {
    broken_install_is_refused("does not match its recorded checksum", |folder, pin| {
        crate::board::state::atomic_write(&folder.join(binary_name(pin)), b"swapped", None)
            .unwrap();
    });
}

#[test]
fn an_extra_file_in_the_installed_runtime_is_refused() {
    // A library planted beside the program would load with it on Windows.
    broken_install_is_refused("does not name", |folder, _| {
        crate::board::state::atomic_create(&folder.join("version.dll"), b"planted").unwrap();
    });
}

#[test]
fn a_missing_installed_file_is_refused() {
    broken_install_is_refused("lacks some", |folder, _| {
        fs::remove_file(folder.join("LICENSE")).unwrap();
    });
}

#[cfg(unix)]
#[test]
fn a_fifo_in_the_installed_runtime_is_refused_without_blocking() {
    broken_install_is_refused("not an owner-only regular file", |folder, _| {
        let path = folder.join("LICENSE");
        fs::remove_file(&path).unwrap();
        rustix::fs::mknodat(
            rustix::fs::CWD,
            &path,
            rustix::fs::FileType::Fifo,
            rustix::fs::Mode::RUSR | rustix::fs::Mode::WUSR,
            0,
        )
        .unwrap();
    });
}

#[test]
fn a_hard_linked_installed_file_is_refused() {
    broken_install_is_refused("one link", |folder, pin| {
        fs::hard_link(
            folder.join(binary_name(pin)),
            folder.parent().unwrap().join("second-name"),
        )
        .unwrap();
    });
}

#[test]
fn an_installed_runtime_that_is_a_file_is_refused() {
    broken_install_is_refused("not a plain folder", |folder, _| {
        let aside = folder.with_extension("old");
        fs::rename(folder, &aside).unwrap();
        crate::board::state::atomic_create(folder, b"not a folder").unwrap();
    });
}

/// A directory link at `link` to `target`: a symlink on POSIX, a junction
/// (made in-process, no `mklink`) on Windows.
fn dir_link(target: &Path, link: &Path) {
    #[cfg(unix)]
    std::os::unix::fs::symlink(target, link).unwrap();
    #[cfg(windows)]
    junction(target, link);
}

/// Create a junction with `FSCTL_SET_REPARSE_POINT`, as `mklink /J` does.
#[cfg(windows)]
#[allow(unsafe_code)]
fn junction(target: &Path, link: &Path) {
    use std::os::windows::{ffi::OsStrExt, fs::OpenOptionsExt, io::AsRawHandle};
    fs::create_dir(link).unwrap();
    let substitute: Vec<u16> = std::ffi::OsStr::new(r"\??\")
        .encode_wide()
        .chain(target.as_os_str().encode_wide())
        .collect();
    let bytes = (substitute.len() * 2) as u16;
    // REPARSE_DATA_BUFFER for IO_REPARSE_TAG_MOUNT_POINT: the substitute
    // name, its NUL, then an empty print name and its NUL.
    let mut buffer = Vec::new();
    buffer.extend_from_slice(&0xA000_0003u32.to_le_bytes());
    buffer.extend_from_slice(&(8 + bytes + 4).to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    buffer.extend_from_slice(&bytes.to_le_bytes());
    buffer.extend_from_slice(&(bytes + 2).to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    for unit in substitute.iter().chain([&0u16, &0u16]) {
        buffer.extend_from_slice(&unit.to_le_bytes());
    }
    let handle = fs::OpenOptions::new()
        .write(true)
        .custom_flags(0x0200_0000 | 0x0020_0000) // BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        .open(link)
        .unwrap();
    let mut returned = 0u32;
    // SAFETY: the handle and the input buffer outlive this synchronous call.
    let ok = unsafe {
        windows_sys::Win32::System::IO::DeviceIoControl(
            handle.as_raw_handle() as _,
            0x0009_00A4, // FSCTL_SET_REPARSE_POINT
            buffer.as_ptr().cast(),
            buffer.len() as u32,
            std::ptr::null_mut(),
            0,
            &mut returned,
            std::ptr::null_mut(),
        )
    };
    assert_ne!(ok, 0, "{}", io::Error::last_os_error());
    let info = fs::symlink_metadata(link).unwrap();
    use std::os::windows::fs::MetadataExt;
    assert_ne!(info.file_attributes() & 0x400, 0, "not a reparse point");
    assert!(
        fs::metadata(link).unwrap().is_dir(),
        "the junction does not resolve"
    );
}

#[test]
fn an_installed_runtime_reached_through_a_link_is_refused() {
    // This user's own link in the owner-only store, one the private-path
    // rule would follow above a private file: the runtime folder itself is
    // never a link.
    broken_install_is_refused("not a plain folder", |folder, _| {
        let real = folder.with_extension("real");
        fs::rename(folder, &real).unwrap();
        dir_link(&real, folder);
    });
}

/// A folder `real` beside the store, and a link to it made by this user in
/// a folder only this user may change: the shape the private-path rule
/// follows. On Windows the folder holding the link gets the protected
/// owner-only access list a hosted runner's temporary folder has (this
/// host's may grant other groups Modify, which would make any link made
/// under it untrusted).
fn own_link(store: &Store, holder: &str) -> (PathBuf, PathBuf) {
    let real = store.base.join("real");
    crate::board::state::private_dir(&real).unwrap();
    let holder = store.base.join(holder);
    crate::board::state::private_dir(&holder).unwrap();
    #[cfg(windows)]
    {
        let me = crate::credentials::windows_security::current_user_sid().unwrap();
        crate::credentials::windows_security::set_link_dacl_sddl(
            &holder,
            &format!("D:P(A;OICI;FA;;;{me})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"),
            true,
        )
        .unwrap();
    }
    let link = holder.join("linked");
    dir_link(&real, &link);
    assert_eq!(crate::private_path::check_path(&link), Ok(()));
    (real, link)
}

#[cfg(unix)]
fn mode_of(path: &Path) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}

#[cfg(unix)]
fn set_mode(path: &Path, bits: u32) {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(bits)).unwrap();
}

/// Every name under `dir`, recursively (empty when it is missing).
fn names_under(dir: &Path) -> Vec<String> {
    let mut out = Vec::new();
    let mut pending = vec![dir.to_owned()];
    while let Some(next) = pending.pop() {
        let Ok(entries) = fs::read_dir(&next) else {
            continue;
        };
        for entry in entries {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_dir() {
                pending.push(entry.path());
            }
            out.push(entry.path().display().to_string());
        }
    }
    out.sort();
    out
}

#[test]
fn a_store_root_behind_this_users_own_link_installs() {
    let store = Store::new();
    let (real, link) = own_link(&store, "home");
    // On POSIX the folder the link leads to may be readable by others, and
    // the store folder in it already exists the same way: the link is this
    // user's, so the store is this user's own folder by another name, and is
    // tightened exactly as it would be if named directly. What the link leads
    // to is a folder above the store, which nothing changes.
    #[cfg(unix)]
    {
        fs::create_dir(real.join("tunnel")).unwrap();
        set_mode(&real.join("tunnel"), 0o755);
        set_mode(&real, 0o755);
    }
    let root = link.join("tunnel");
    let archive = release(&host());
    let pin = pin_of(&archive);
    let probe = Fake::new(0);
    let installed = ensure_with(&root, pin, &probe, |_, _| Ok(archive.clone())).unwrap();
    assert!(installed.changed);
    assert_eq!(
        installed.binary,
        root.join(folder_name(&pin)).join(binary_name(&pin))
    );
    assert_eq!(probe.calls.borrow().len(), 1);
    // The files are where the link leads.
    let wrapper = real
        .join("tunnel")
        .join(folder_name(&pin))
        .join(binary_name(&pin));
    assert_eq!(fs::read(&wrapper).unwrap(), b"wrapper program");
    #[cfg(unix)]
    {
        assert_eq!(
            mode_of(&real.join("tunnel")),
            0o700,
            "the store was not made owner-only"
        );
        assert_eq!(
            mode_of(&real),
            0o755,
            "the folder above the store was changed"
        );
    }
    // A rerun through the same link reuses it.
    let again = ensure_with(
        &root,
        pin,
        &Fake::new(0),
        |_, _| -> Result<Vec<u8>, String> { panic!("a verified runtime is downloaded again") },
    )
    .unwrap();
    assert!(!again.changed);
}

#[test]
fn a_store_root_behind_a_link_another_account_could_change_is_refused_and_nothing_is_written() {
    let store = Store::new();
    let (real, link) = own_link(&store, "shared");
    let holder = link.parent().unwrap().to_owned();
    #[cfg(unix)]
    {
        fs::create_dir(real.join("tunnel")).unwrap();
        set_mode(&real.join("tunnel"), 0o755);
    }
    let root = link.join("tunnel");
    let archive = release(&host());
    let pin = pin_of(&archive);
    let probe = Fake::new(0);
    // The shapes of #1644's own refusals: the link's folder writable by
    // others without the sticky bit (POSIX), or where another account may
    // delete entries (Windows); and on Windows a junction another account may
    // rewrite.
    #[cfg(unix)]
    let shapes: Vec<Box<dyn Fn() + '_>> = vec![Box::new(|| set_mode(&holder, 0o777))];
    #[cfg(windows)]
    let me = crate::credentials::windows_security::current_user_sid().unwrap();
    #[cfg(windows)]
    let shapes: Vec<Box<dyn Fn() + '_>> = vec![
        Box::new(|| {
            crate::credentials::windows_security::set_link_dacl_sddl(
                &holder,
                &format!("D:P(A;OICI;FA;;;{me})(A;OICI;FA;;;SY)(A;;0x40;;;AU)"),
                true,
            )
            .unwrap()
        }),
        Box::new(|| {
            crate::credentials::windows_security::set_link_dacl_sddl(
                &link,
                &format!("D:P(A;;FA;;;{me})(A;;0x1301bf;;;AU)"),
                true,
            )
            .unwrap()
        }),
    ];
    let trusted = || {
        #[cfg(unix)]
        set_mode(&holder, 0o700);
        #[cfg(windows)]
        for path in [&holder, &link] {
            crate::credentials::windows_security::set_link_dacl_sddl(
                path,
                &format!("D:P(A;OICI;FA;;;{me})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"),
                true,
            )
            .unwrap();
        }
    };
    let before = names_under(&real);
    for make_hostile in &shapes {
        make_hostile();
        // The private-path rule refuses at the link itself.
        let refusal = crate::private_path::check_path(&root).unwrap_err();
        assert_eq!(refusal.element, link, "{refusal}");
        let error = ensure_with(&root, pin, &probe, |_, _| Ok(archive.clone())).unwrap_err();
        assert!(
            error.contains("could be changed by another account"),
            "{error}"
        );
        assert_eq!(names_under(&real), before, "something was written");
        #[cfg(unix)]
        assert_eq!(
            mode_of(&real.join("tunnel")),
            0o755,
            "the store was changed"
        );
        assert!(probe.calls.borrow().is_empty());
        trusted();
    }
    // The twin: the same link, its folder made this user's alone again,
    // installs.
    ensure_with(&root, pin, &probe, |_, _| Ok(archive.clone())).unwrap();
    assert_eq!(probe.calls.borrow().len(), 1);
}

#[test]
fn a_store_root_that_is_itself_a_link_is_refused_and_nothing_is_written() {
    // Even this user's own link, which the private-path rule would follow:
    // the tunnel store's other readers (`store::checked_path`) refuse a
    // store that is a link, and nothing is changed through it.
    let store = Store::new();
    let (real, link) = own_link(&store, "home");
    #[cfg(unix)]
    set_mode(&real, 0o755);
    let probe = Fake::new(0);
    let archive = release(&host());
    let error =
        ensure_with(&link, pin_of(&archive), &probe, |_, _| Ok(archive.clone())).unwrap_err();
    assert!(
        error.contains("is not a plain folder only your account"),
        "{error}"
    );
    assert!(names_under(&real).is_empty(), "{:?}", names_under(&real));
    assert!(probe.calls.borrow().is_empty());
    #[cfg(unix)]
    assert_eq!(
        mode_of(&real),
        0o755,
        "the folder behind the link was changed"
    );
}
