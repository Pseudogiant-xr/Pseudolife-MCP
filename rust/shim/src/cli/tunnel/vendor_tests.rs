//! In-process tests of the vendor runtime: archives are built in memory and
//! injected, the download is a closure and the `--help` probe a fake
//! `Runner`, so nothing here touches the network or starts a process.
use std::{cell::RefCell, io::Write as _};

use zip::write::SimpleFileOptions;

use super::*;
use crate::cli::update::clients::Output;

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

/// The `--help` probe: answers `code`, records each call and checks the
/// program it is asked to run exists at that moment.
struct Probe {
    code: i32,
    calls: RefCell<Vec<(PathBuf, Vec<String>)>>,
    during: Box<dyn Fn(&Path)>,
}
impl Probe {
    fn new(code: i32) -> Self {
        Self {
            code,
            calls: RefCell::new(Vec::new()),
            during: Box::new(|_| {}),
        }
    }
}
impl Runner for Probe {
    fn run(
        &self,
        executable: &Path,
        args: &[&str],
        _cwd: Option<&Path>,
        _timeout: Duration,
        _env: &[(&str, &str)],
    ) -> Output {
        assert!(executable.is_file(), "probe of a missing {executable:?}");
        (self.during)(executable);
        self.calls.borrow_mut().push((
            executable.to_owned(),
            args.iter().map(|a| a.to_string()).collect(),
        ));
        Output {
            code: self.code,
            bytes: vec![],
        }
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

/// A zip of `(name, bytes, unix mode)` members, deflated, the way the vendor
/// builds them.
fn zip_of(members: &[(&str, &[u8], u32)]) -> Vec<u8> {
    let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
    for (name, data, mode) in members {
        let options = SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated)
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
    probe: &Probe,
) -> Result<Installed, String> {
    install(&store.root, pin_of(pinned), archive, probe, UNPACKED_LIMIT)
}

/// The archive is refused, the probe never ran and the store is as before.
fn refused(archive: &[u8], fragment: &str) {
    let store = Store::new();
    let before = store.listing();
    let probe = Probe::new(0);
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
    let probe = Probe::new(0);
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
    // One probe, of the staged wrapper, with --help only.
    let calls = probe.calls.borrow();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].1, ["--help"]);
    let staged = calls[0].0.parent().unwrap();
    assert_eq!(staged.parent().unwrap(), store.root);
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
    ensure_with(&store.root, pin, &Probe::new(0), |_, _| Ok(archive.clone())).unwrap();
    let before = store.listing();
    let folder = store.root.join(folder_name(&pin));
    let manifest = fs::read(folder.join(MANIFEST)).unwrap();
    let probe = Probe::new(0);
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
fn a_runtime_the_python_client_installed_is_reused() {
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
        let comma = if index == 0 { "" } else { ", " };
        // json.dumps's spacing.
        manifest += &format!("{comma}\"{name}\": \"{:x}\"", Sha256::digest(data));
    }
    manifest += "}";
    crate::board::state::atomic_create(&folder.join(MANIFEST), manifest.as_bytes()).unwrap();
    let installed = ensure_with(
        &store.root,
        pin,
        &Probe::new(0),
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
    let probe = Probe::new(0);
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
    let error = attempt(&store, &archive, short, &Probe::new(0)).unwrap_err();
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
    let error = attempt(&store, &archive, &long, &Probe::new(0)).unwrap_err();
    assert!(error.contains("larger than the pinned release"), "{error}");
    assert!(store.listing().is_empty());
}

#[test]
fn a_fetch_failure_is_returned_and_leaves_nothing() {
    let store = Store::new();
    let pin = host();
    let error = ensure_with(&store.root, pin, &Probe::new(0), |_, _| {
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
fn the_unpacked_total_is_capped_by_what_actually_unpacks() {
    let store = Store::new();
    let wrapper = binary_name(&host());
    let archive = zip_of(&[
        (wrapper, &[7u8; 4096], REGULAR),
        ("LICENSE", &[0u8; 4096], REGULAR),
    ]);
    let pin = pin_of(&archive);
    // Declared sizes over the cap: refused before a stage exists.
    let error = install(&store.root, pin, &archive, &Probe::new(0), 4096).unwrap_err();
    assert!(error.contains("more than the size cap"), "{error}");
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
    let error = install(&store.root, pin, &lying, &Probe::new(0), 4096 + 2).unwrap_err();
    assert!(error.contains("nothing was installed"), "{error}");
    assert!(store.listing().is_empty(), "{:?}", store.listing());
}

#[test]
fn a_failed_probe_is_refused_and_leaves_nothing() {
    let store = Store::new();
    let archive = release(&host());
    let probe = Probe::new(3);
    let error = attempt(&store, &archive, &archive, &probe).unwrap_err();
    assert!(
        error.contains("did not start (`tunnel-client --help` exited 3)"),
        "{error}"
    );
    assert_eq!(probe.calls.borrow().len(), 1);
    assert!(store.listing().is_empty(), "{:?}", store.listing());
}

#[test]
fn an_install_that_finished_meanwhile_is_kept_and_the_stage_removed() {
    let store = Store::new();
    let archive = release(&host());
    let pin = pin_of(&archive);
    // A first install, moved aside to stand in for a concurrent one.
    install(&store.root, pin, &archive, &Probe::new(0), UNPACKED_LIMIT).unwrap();
    let folder = store.root.join(folder_name(&pin));
    let aside = store.root.join("aside");
    fs::rename(&folder, &aside).unwrap();
    let mut probe = Probe::new(0);
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
    install(&store.root, pin, &archive, &Probe::new(0), UNPACKED_LIMIT).unwrap();
    let folder = store.root.join(folder_name(&pin));
    break_it(&folder, &pin);
    let before = store.listing();
    let error = ensure_with(
        &store.root,
        pin,
        &Probe::new(0),
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
    broken_install_is_refused("not a plain folder", |folder, _| {
        let real = folder.with_extension("real");
        fs::rename(folder, &real).unwrap();
        dir_link(&real, folder);
    });
}

#[test]
fn a_store_root_reached_through_a_link_is_refused_and_nothing_is_written() {
    let store = Store::new();
    let link = store.base.join("linked");
    dir_link(&store.root, &link);
    let probe = Probe::new(0);
    let archive = release(&host());
    let error = ensure_with(&link.join("inner"), pin_of(&archive), &probe, |_, _| {
        Ok(archive.clone())
    })
    .unwrap_err();
    assert!(error.contains("link or junction is on its path"), "{error}");
    let error =
        ensure_with(&link, pin_of(&archive), &probe, |_, _| Ok(archive.clone())).unwrap_err();
    assert!(error.contains("link or junction is on its path"), "{error}");
    assert!(store.listing().is_empty(), "{:?}", store.listing());
    assert!(probe.calls.borrow().is_empty());
}
