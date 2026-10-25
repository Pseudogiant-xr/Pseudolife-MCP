use super::*;
use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::OsString;
use std::io::Write;

const TARGETS: [&str; 7] = [
    "windows-x86_64",
    "windows-aarch64",
    "linux-x86_64-gnu",
    "linux-x86_64-musl",
    "linux-aarch64-gnu",
    "macos-aarch64",
    "macos-x86_64",
];

fn is_sha256(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn sha256(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    native::hex(&Sha256::digest(bytes))
}

#[test]
fn every_target_is_pinned_by_archive_and_library_hash() {
    let pins = all_pins();
    assert_eq!(pins.tag, "chromium/8076");
    assert_eq!(pins.build, "8076");
    for target in TARGETS {
        let pin = pins
            .target(target)
            .unwrap_or_else(|| panic!("{target} pinned"));
        assert!(is_sha256(&pin.archive_sha256), "{target} archive");
        assert!(is_sha256(&pin.library_sha256), "{target} library");
        assert!(
            pin.url.starts_with(
                "https://github.com/bblanchon/pdfium-binaries/releases/download/chromium/8076/"
            ),
            "{target} url {}",
            pin.url
        );
        assert!(pin.url.ends_with(".tgz"), "{target}");
    }
    assert!(pins.target("plan9-mips").is_none());
}

#[test]
fn the_library_path_matches_the_platform() {
    let pins = all_pins();
    let lib = |t: &str| pins.target(t).unwrap().library.clone();
    assert_eq!(lib("windows-x86_64"), "bin/pdfium.dll");
    assert_eq!(lib("linux-x86_64-gnu"), "lib/libpdfium.so");
    assert_eq!(lib("macos-aarch64"), "lib/libpdfium.dylib");
}

#[test]
fn this_build_has_a_pin_on_every_ci_platform() {
    // The hosted runners and dev machines are all pinned targets.
    let here = this_target().expect("a pinned target for this build");
    assert!(TARGETS.contains(&here), "{here}");
    let build = Build::current().expect("this build");
    assert_eq!(build.pin.url, all_pins().target(here).unwrap().url);
    assert_eq!(build.tag, "chromium/8076");
}

#[test]
fn the_python_lock_names_the_pypdfium2_this_pin_was_checked_against() {
    // pin_pdfium.py checked that this pypdfium2 bundles the pinned build; the PDF differential
    // compares the two daemons byte for byte, so the pins move together.
    let pin: serde_json::Value = serde_json::from_str(PIN_JSON).unwrap();
    let oracle = pin["oracle_pypdfium2"].as_str().expect("oracle_pypdfium2");
    let lock = include_str!("../../../../ops/requirements.lock.txt");
    let locked: Vec<&str> = lock
        .lines()
        .filter_map(|l| l.trim().strip_prefix("pypdfium2=="))
        .collect();
    assert_eq!(locked, [oracle], "ops/requirements.lock.txt pypdfium2");
}

// ── which archive entries are kept (pure) ───────────────────────────────

fn build_for(library: &str, archive: &[u8], lib_bytes: &[u8]) -> Build {
    Build {
        tag: "chromium/9999".into(),
        build: "9999".into(),
        pin: TargetPin {
            url: "https://example.com/pdfium-test.tgz".into(),
            archive_sha256: sha256(archive),
            library: library.into(),
            library_sha256: sha256(lib_bytes),
        },
    }
}

#[test]
fn only_the_library_and_its_licenses_are_kept() {
    let b = build_for("lib/libpdfium.so", b"", b"");
    let kept = |raw: &str| b.destination(raw);
    assert_eq!(
        kept("lib/libpdfium.so"),
        Some(PathBuf::from("libpdfium.so"))
    );
    assert_eq!(
        kept("./lib/libpdfium.so"),
        Some(PathBuf::from("libpdfium.so"))
    );
    assert_eq!(kept("LICENSE"), Some(PathBuf::from("LICENSE")));
    assert_eq!(
        kept("licenses/icu.txt"),
        Some(Path::new("licenses").join("icu.txt"))
    );
    for skipped in [
        "VERSION",
        "args.gn",
        "include/fpdfview.h",
        "lib/",
        "licenses/",
        "licenses/../escape.txt",
        "licenses/sub/deeper.txt",
        "licenses/.hidden",
        "licenses/c:stream",
        "licenses/a\\b.txt",
        "/lib/libpdfium.so",
        "bin/pdfium.dll",
        "lib/libpdfium.so.evil",
    ] {
        assert_eq!(kept(skipped), None, "{skipped}");
    }
}

#[test]
fn the_install_dir_changes_with_the_archive_pin() {
    let root = Path::new("root");
    let a = build_for("lib/libpdfium.so", b"one", b"lib");
    let b = build_for("lib/libpdfium.so", b"two", b"lib");
    let dir = a.install_dir(root);
    let name = dir.file_name().unwrap().to_str().unwrap().to_string();
    assert!(
        name.starts_with("9999-") && name.len() == "9999-".len() + 12,
        "{name}"
    );
    assert_ne!(dir, b.install_dir(root));
}

#[test]
fn the_root_is_the_override_or_beside_the_lite_runtime() {
    let env = |pairs: &'static [(&'static str, &'static str)]| {
        move |name: &str| {
            pairs
                .iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| OsString::from(*v))
        }
    };
    let home = Path::new("/home/u");
    assert_eq!(
        default_root(
            Platform::Other,
            &env(&[(ROOT_ENV, "/opt/pdfium")]),
            Some(home)
        ),
        Some(PathBuf::from("/opt/pdfium"))
    );
    assert_eq!(
        default_root(Platform::Other, &env(&[(ROOT_ENV, "")]), Some(home)),
        Some(home.join(".local/share/pseudolife-mcp/native/pdfium"))
    );
    assert_eq!(
        default_root(
            Platform::Other,
            &env(&[("XDG_DATA_HOME", "/data")]),
            Some(home)
        ),
        Some(PathBuf::from("/data/pseudolife-mcp/native/pdfium"))
    );
    assert_eq!(
        default_root(Platform::Windows, &env(&[("LOCALAPPDATA", r"C:\L")]), None),
        Some(
            PathBuf::from(r"C:\L")
                .join("pseudolife-mcp")
                .join("native")
                .join("pdfium")
        )
    );
    assert_eq!(default_root(Platform::Other, &env(&[]), None), None);
}

#[test]
fn each_refusal_names_its_cause_and_only_the_fix_that_works() {
    let cmd = "`pseudolife-daemon provision pdfium`";
    let lib = |build, root| library_at(build, root).unwrap_err();
    // No pinned build: nothing to run.
    let m = lib(
        Err("no PDFium build is pinned for linux-aarch64-musl".into()),
        None,
    );
    assert!(
        m.starts_with("pdf_engine_unavailable: reading a PDF needs"),
        "{m}"
    );
    assert!(m.contains("pinned for linux-aarch64-musl"), "{m}");
    assert!(!m.contains("provision"), "{m}");
    // No install root: set one, then provision.
    let build = build_for("lib/libpdfium.so", b"archive", LIB);
    let m = lib(Ok(build.clone()), None);
    assert!(
        m.contains(&format!("set {ROOT_ENV} to one and run {cmd}, then ingest")),
        "{m}"
    );
    // Not installed: provision.
    let root = scratch("refusal");
    let m = lib(Ok(build.clone()), Some(root.clone()));
    assert!(
        m.contains(&format!(
            "it is not installed at {}: run {cmd}, then ingest the file again; text",
            build.install_dir(&root).display()
        )),
        "{m}"
    );
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_mismatched_install_is_refused_with_one_fix() {
    let root = scratch("mismatch-refusal");
    let body = archive();
    let build = build_for("lib/libpdfium.so", &body, LIB);
    let (path, _) = build
        .provision(&root, &fetch_serving(&build, body), &mut |_| {})
        .unwrap();
    assert_eq!(
        library_at(Ok(build.clone()), Some(root.clone())),
        Ok(path.clone())
    );
    fs::write(&path, b"swapped").unwrap();
    let m = library_at(Ok(build.clone()), Some(root.clone())).unwrap_err();
    assert!(m.contains("does not match this build ("), "{m}");
    assert!(
        m.contains("): move that directory aside and run `pseudolife-daemon provision pdfium`"),
        "{m}"
    );
    assert_eq!(m.matches("aside").count(), 1, "{m}");
    assert!(!m.contains(".."), "{m}");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn the_platform_label_names_the_c_library_on_linux() {
    let label = platform_label();
    if cfg!(target_os = "linux") {
        assert!(
            label.ends_with("-gnu") || label.ends_with("-musl"),
            "{label}"
        );
    } else {
        assert_eq!(
            label,
            format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH)
        );
    }
}

// ── provisioning a synthetic archive (runs the shared provisioner) ────────

#[derive(Default)]
struct FakeFetch {
    files: HashMap<String, Vec<u8>>,
    calls: RefCell<Vec<String>>,
}

impl Fetch for FakeFetch {
    fn fetch(&self, url: &str, sink: &mut dyn Write) -> Result<(), String> {
        self.calls.borrow_mut().push(url.to_owned());
        let body = self.files.get(url).ok_or("404 Not Found")?;
        sink.write_all(body).map_err(|e| e.to_string())
    }
}

fn tar_gz(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(
        Vec::new(),
        flate2::Compression::fast(),
    ));
    for (path, body) in entries {
        let mut header = tar::Header::new_gnu();
        if path.ends_with('/') {
            header.set_entry_type(tar::EntryType::Directory);
            header.set_size(0);
        } else {
            header.set_entry_type(tar::EntryType::Regular);
            header.set_size(body.len() as u64);
        }
        header.set_mode(0o644);
        header.set_path(path).unwrap();
        header.set_cksum();
        builder.append(&header, *body).unwrap();
    }
    builder.into_inner().unwrap().finish().unwrap()
}

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "pl-pdfium-{label}-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

const LIB: &[u8] = b"\x7fELF not really a library";

fn archive() -> Vec<u8> {
    tar_gz(&[
        ("LICENSE", b"MIT"),
        ("VERSION", b"BUILD=9999"),
        ("include/", b""),
        ("include/fpdfview.h", b"// header"),
        ("lib/", b""),
        ("lib/libpdfium.so", LIB),
        ("licenses/", b""),
        ("licenses/icu.txt", b"ICU license"),
    ])
}

fn fetch_serving(build: &Build, body: Vec<u8>) -> FakeFetch {
    let mut fetch = FakeFetch::default();
    fetch.files.insert(build.pin.url.clone(), body);
    fetch
}

fn names(dir: &Path) -> Vec<String> {
    let mut out: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    out.sort();
    out
}

#[test]
fn provisioning_installs_the_pinned_library_once() {
    let root = scratch("install");
    let body = archive();
    let build = build_for("lib/libpdfium.so", &body, LIB);
    let fetch = fetch_serving(&build, body);
    assert_eq!(build.locate(&root).unwrap(), None);

    let mut lines = Vec::new();
    let (path, installed) = build
        .provision(&root, &fetch, &mut |l| lines.push(l.to_string()))
        .unwrap();
    assert!(installed);
    let dir = build.install_dir(&root);
    assert_eq!(path, dir.join("libpdfium.so"));
    assert_eq!(fs::read(&path).unwrap(), LIB);
    assert_eq!(
        names(&dir),
        ["LICENSE", "libpdfium.so", "licenses", native::PROVENANCE]
    );
    assert_eq!(names(&dir.join("licenses")), ["icu.txt"]);
    assert!(
        lines
            .last()
            .unwrap()
            .starts_with("installed the PDFium chromium/9999 library at ")
    );
    assert_eq!(build.locate(&root).unwrap(), Some(path.clone()));

    // A second run changes nothing and says so in one line.
    let mut again = Vec::new();
    let (same, installed) = build
        .provision(&root, &fetch, &mut |l| again.push(l.to_string()))
        .unwrap();
    assert!(!installed);
    assert_eq!(same, path);
    assert_eq!(again.len(), 1, "{again:?}");
    assert!(again[0].starts_with("PDFium chromium/9999 library already installed at "));
    assert_eq!(fetch.calls.borrow().len(), 1);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_wrong_archive_or_library_hash_installs_nothing() {
    let body = archive();
    // The archive differs from its pin.
    let root = scratch("archive-hash");
    let mut build = build_for("lib/libpdfium.so", &body, LIB);
    build.pin.archive_sha256 = "0".repeat(64);
    let err = build
        .provision(&root, &fetch_serving(&build, body.clone()), &mut |_| {})
        .unwrap_err();
    assert!(matches!(err, Error::Checksum { .. }), "{err}");
    assert_eq!(names(&root), [".install.lock"]);
    assert_eq!(build.locate(&root).unwrap(), None);
    let _ = fs::remove_dir_all(&root);

    // The archive matches, the library inside it does not.
    let root = scratch("library-hash");
    let build = build_for("lib/libpdfium.so", &body, b"another library");
    let err = build
        .provision(&root, &fetch_serving(&build, body.clone()), &mut |_| {})
        .unwrap_err();
    assert!(matches!(err, Error::Incomplete { .. }), "{err}");
    assert!(err.to_string().contains("not the pinned"), "{err}");
    assert_eq!(names(&root), [".install.lock"]);
    let _ = fs::remove_dir_all(&root);

    // The pinned library is missing from the archive.
    let root = scratch("library-missing");
    let build = build_for("lib/libpdfium.dylib", &body, LIB);
    let err = build
        .provision(&root, &fetch_serving(&build, body), &mut |_| {})
        .unwrap_err();
    assert!(matches!(err, Error::Incomplete { .. }), "{err}");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_library_link_in_the_archive_is_refused() {
    let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(
        Vec::new(),
        flate2::Compression::fast(),
    ));
    let mut header = tar::Header::new_gnu();
    header.set_entry_type(tar::EntryType::Symlink);
    header.set_size(0);
    header.set_mode(0o777);
    header.set_path("lib/libpdfium.so").unwrap();
    header.set_link_name("/etc/passwd").unwrap();
    header.set_cksum();
    builder.append(&header, &b""[..]).unwrap();
    let body = builder.into_inner().unwrap().finish().unwrap();
    let root = scratch("link");
    let build = build_for("lib/libpdfium.so", &body, LIB);
    let err = build
        .provision(&root, &fetch_serving(&build, body), &mut |_| {})
        .unwrap_err();
    assert!(err.to_string().contains("is not a regular file"), "{err}");
    assert_eq!(names(&root), [".install.lock"]);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_changed_library_is_found_and_refused() {
    let root = scratch("tampered");
    let body = archive();
    let build = build_for("lib/libpdfium.so", &body, LIB);
    let (path, _) = build
        .provision(&root, &fetch_serving(&build, body), &mut |_| {})
        .unwrap();
    fs::write(&path, b"swapped").unwrap();
    let err = build.locate(&root).unwrap_err();
    assert!(matches!(err, Error::Installed { .. }), "{err}");
    assert!(err.to_string().contains("not the pinned"), "{err}");
    let _ = fs::remove_dir_all(&root);
}
