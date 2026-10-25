use super::*;
use std::{
    cell::RefCell,
    collections::HashMap,
    io::{self, Read, Write},
};

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "pl-pgruntime-{label}-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    ));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn leak(text: String) -> &'static str {
    Box::leak(text.into_boxed_str())
}

fn hex_sha256(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Serves fixed bytes per URL and records each request.
#[derive(Default)]
struct FakeFetch {
    files: HashMap<String, Vec<u8>>,
    fail: Option<String>,
    calls: RefCell<Vec<String>>,
}
impl Fetch for FakeFetch {
    fn fetch(&self, url: &str, sink: &mut dyn Write) -> Result<(), String> {
        self.calls.borrow_mut().push(url.to_owned());
        if self.fail.as_deref() == Some(url) {
            return Err("connection refused".into());
        }
        let body = self.files.get(url).ok_or("404 Not Found")?;
        // Two writes, so a sink that only sees the first chunk is caught.
        let (a, b) = body.split_at(body.len() / 2);
        sink.write_all(a).map_err(|e| e.to_string())?;
        sink.write_all(b).map_err(|e| e.to_string())
    }
}

fn exe(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_owned()
    }
}

enum Entry<'a> {
    File(&'a str, &'a [u8], u32),
    Dir(&'a str),
    Symlink(&'a str, &'a str),
}

fn tar_gz(entries: &[Entry]) -> Vec<u8> {
    let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(
        Vec::new(),
        flate2::Compression::fast(),
    ));
    for entry in entries {
        let mut header = tar::Header::new_gnu();
        match entry {
            Entry::File(path, body, mode) => {
                header.set_entry_type(tar::EntryType::Regular);
                header.set_size(body.len() as u64);
                header.set_mode(*mode);
                set_raw_path(&mut header, path);
                header.set_cksum();
                builder.append(&header, *body).unwrap();
            }
            Entry::Dir(path) => {
                header.set_entry_type(tar::EntryType::Directory);
                header.set_size(0);
                header.set_mode(0o755);
                set_raw_path(&mut header, path);
                header.set_cksum();
                builder.append(&header, &[][..]).unwrap();
            }
            Entry::Symlink(path, target) => {
                header.set_entry_type(tar::EntryType::Symlink);
                header.set_size(0);
                header.set_mode(0o777);
                set_raw_path(&mut header, path);
                let name = &mut header.as_old_mut().linkname;
                name.fill(0);
                name[..target.len()].copy_from_slice(target.as_bytes());
                header.set_cksum();
                builder.append(&header, &[][..]).unwrap();
            }
        }
    }
    builder.into_inner().unwrap().finish().unwrap()
}

/// Writes the name bytes directly, so a test can build the hostile names
/// (`..`, absolute) that `Header::set_path` refuses to write.
fn set_raw_path(header: &mut tar::Header, path: &str) {
    let name = &mut header.as_old_mut().name;
    name.fill(0);
    name[..path.len()].copy_from_slice(path.as_bytes());
}

fn zip_of(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut writer = zip::ZipWriter::new(io::Cursor::new(Vec::new()));
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);
    for (path, body) in entries {
        if path.ends_with('/') {
            writer.add_directory(*path, options).unwrap();
        } else {
            writer.start_file(*path, options).unwrap();
            writer.write_all(body).unwrap();
        }
    }
    writer.finish().unwrap().into_inner()
}

/// A theseus-shaped archive: one top directory holding the binaries.
fn postgresql_archive(zip: bool) -> Vec<u8> {
    let top = "postgresql-18.1.0-test";
    let names: Vec<String> = ["initdb", "pg_ctl", "postgres", "psql"]
        .iter()
        .map(|n| format!("{top}/bin/{}", exe(n)))
        .collect();
    if zip {
        let mut entries: Vec<(&str, &[u8])> = vec![("postgresql-18.1.0-test/", b"")];
        entries.extend(names.iter().map(|n| (n.as_str(), &b"binary"[..])));
        entries.push(("postgresql-18.1.0-test/share/README", b"readme"));
        entries.push(("postgresql-18.1.0-test/lib/libpq.so.5.18", b"libpq"));
        zip_of(&entries)
    } else {
        let mut entries = vec![
            Entry::Dir("postgresql-18.1.0-test/"),
            Entry::Dir("postgresql-18.1.0-test/bin/"),
        ];
        entries.extend(names.iter().map(|n| Entry::File(n, b"binary", 0o755)));
        entries.push(Entry::File(
            "postgresql-18.1.0-test/share/README",
            b"readme",
            0o666,
        ));
        entries.push(Entry::File(
            "postgresql-18.1.0-test/lib/libpq.so.5.18",
            b"libpq",
            0o755,
        ));
        entries.push(Entry::Symlink(
            "postgresql-18.1.0-test/lib/libpq.so.5",
            "libpq.so.5.18",
        ));
        tar_gz(&entries)
    }
}

/// A pgvector_compiled-shaped archive (its files are mode 0777 upstream).
fn pgvector_archive() -> Vec<u8> {
    tar_gz(&[
        Entry::Dir("pgvector-test/"),
        Entry::File("pgvector-test/lib/vector.so", b"vector library", 0o777),
        Entry::File("pgvector-test/lib/bitcode/vector/src/hnsw.bc", b"bc", 0o777),
        Entry::File(
            "pgvector-test/include/server/extension/vector/vector.h",
            b"header",
            0o777,
        ),
        Entry::File(
            "pgvector-test/share/extension/vector.control",
            b"default_version = '0.8.5'",
            0o777,
        ),
        Entry::File(
            "pgvector-test/share/extension/vector--0.8.5.sql",
            b"CREATE TYPE vector;",
            0o777,
        ),
    ])
}

fn zstd(bytes: &[u8]) -> Vec<u8> {
    ruzstd::encoding::compress_to_vec(bytes, ruzstd::encoding::CompressionLevel::Fastest)
}

fn ar(members: &[(&str, &[u8])]) -> Vec<u8> {
    let mut out = b"!<arch>\n".to_vec();
    for (name, body) in members {
        out.extend(
            format!(
                "{name:<16}{:<12}{:<6}{:<6}{:<8}{:<10}`\n",
                0,
                0,
                0,
                100644,
                body.len()
            )
            .as_bytes(),
        );
        out.extend_from_slice(body);
        if body.len() % 2 == 1 {
            out.push(b'\n');
        }
    }
    out
}

/// An Ubuntu-shaped .deb: `data.tar.zst` holding the versioned library and
/// its SONAME symlink, as the jammy libxml2 package does.
fn deb_with(data_name: &str, data: Vec<u8>) -> Vec<u8> {
    ar(&[
        ("debian-binary", b"2.0\n"),
        ("control.tar.zst", &zstd(&tar_plain(&[]))),
        (data_name, &data),
    ])
}

fn tar_plain(entries: &[Entry]) -> Vec<u8> {
    let gz = tar_gz(entries);
    let mut out = Vec::new();
    flate2::read::GzDecoder::new(&gz[..])
        .read_to_end(&mut out)
        .unwrap();
    out
}

fn libxml2_deb() -> Vec<u8> {
    deb_with(
        "data.tar.zst",
        zstd(&tar_plain(&[
            Entry::Dir("./usr/lib/x86_64-linux-gnu/"),
            Entry::File(
                "./usr/lib/x86_64-linux-gnu/libxml2.so.2.9.13",
                b"libxml2 bytes",
                0o644,
            ),
            Entry::Symlink(
                "./usr/lib/x86_64-linux-gnu/libxml2.so.2",
                "libxml2.so.2.9.13",
            ),
            Entry::File("./usr/share/doc/libxml2/copyright", b"(c)", 0o644),
        ])),
    )
}

const LIBXML2: &[&str] = &["libxml2.so.2.9.13"];

/// Fake pins served by a `FakeFetch`: (artifacts, fetch).
fn fake_set(zip: bool, with_deb: bool) -> (&'static [Artifact], FakeFetch) {
    let mut fetch = FakeFetch::default();
    let mut artifacts = Vec::new();
    let mut add = |url: String, body: Vec<u8>, unpack: Unpack| {
        artifacts.push(Artifact {
            url: leak(url.clone()),
            sha256: leak(hex_sha256(&body)),
            unpack,
        });
        fetch.files.insert(url, body);
    };
    let ext = if zip { "zip" } else { "tar.gz" };
    add(
        format!("https://example.com/postgresql-18.1.0-test.{ext}"),
        postgresql_archive(zip),
        Unpack::PostgreSql,
    );
    add(
        "https://example.com/pgvector-test-pg18.tar.gz".into(),
        pgvector_archive(),
        Unpack::Pgvector,
    );
    if with_deb {
        add(
            "https://example.com/libxml2.deb".into(),
            libxml2_deb(),
            Unpack::Deb(LIBXML2),
        );
    }
    (Box::leak(artifacts.into_boxed_slice()), fetch)
}

fn quiet() -> impl FnMut(&str) {
    |_| {}
}

fn staging_left(root: &Path) -> Vec<String> {
    fs::read_dir(root)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with(".staging-"))
        .collect()
}

// ── pins ────────────────────────────────────────────────────────────────

#[test]
fn the_pins_cover_every_supported_platform_with_pg0_s_artifacts() {
    for (os, arch, env, triple, debs) in [
        ("windows", "x86_64", "msvc", "x86_64-pc-windows-msvc", 0),
        ("linux", "x86_64", "gnu", "x86_64-unknown-linux-gnu", 2),
        ("linux", "aarch64", "gnu", "aarch64-unknown-linux-gnu", 2),
        ("macos", "aarch64", "", "aarch64-apple-darwin", 0),
        ("macos", "x86_64", "", "x86_64-apple-darwin", 0),
    ] {
        let pins = artifacts_for(os, arch, env).unwrap_or_else(|| panic!("{triple}"));
        assert_eq!(pins[0].unpack, Unpack::PostgreSql, "{triple}");
        assert_eq!(pins[1].unpack, Unpack::Pgvector, "{triple}");
        assert_eq!(pins.len(), 2 + debs, "{triple}");
        let ext = if os == "windows" { "zip" } else { "tar.gz" };
        assert_eq!(
            pins[0].url,
            format!(
                "https://github.com/theseus-rs/postgresql-binaries/releases/download/18.1.0/\
                 postgresql-18.1.0-{triple}.{ext}"
            )
        );
        assert_eq!(
            pins[1].url,
            format!(
                "https://github.com/nicoloboschi/pgvector_compiled/releases/download/v0.18.320/\
                 pgvector-{triple}-pg18.tar.gz"
            )
        );
        for pin in pins {
            assert!(pin.url.starts_with("https://"), "{}", pin.url);
            assert_eq!(pin.sha256.len(), 64, "{}", pin.url);
            assert!(
                pin.sha256
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
                "{}",
                pin.url
            );
        }
        for pin in &pins[2..] {
            assert!(matches!(pin.unpack, Unpack::Deb(_)), "{triple}");
            assert!(
                pin.url.starts_with("https://launchpadlibrarian.net/"),
                "{}",
                pin.url
            );
        }
    }
}

#[test]
fn unpinned_platforms_are_refused_naming_the_server_tier() {
    assert!(artifacts_for("linux", "x86_64", "musl").is_none());
    assert!(artifacts_for("windows", "aarch64", "msvc").is_none());
    assert!(artifacts_for("freebsd", "x86_64", "").is_none());
    let error = Unsupported {
        os: "linux".into(),
        arch: "x86_64".into(),
        env: "musl".into(),
    };
    let text = error.to_string();
    assert!(text.contains("linux/x86_64 (musl)"), "{text}");
    assert!(text.contains("PSEUDOLIFE_MCP_DATABASE_URL"), "{text}");
}

#[test]
fn the_install_dir_is_versioned_and_changes_with_any_pin() {
    let root = Path::new("runtimes");
    let (pins, _) = fake_set(false, true);
    let dir = install_dir(root, pins);
    let name = dir.file_name().unwrap().to_string_lossy().into_owned();
    assert!(name.starts_with("18.1.0-"), "{name}");
    assert_eq!(name.len(), "18.1.0-".len() + 12, "{name}");
    assert_eq!(dir.parent(), Some(root));
    let mut changed = pins.to_vec();
    changed[2].sha256 = leak("0".repeat(64));
    assert_ne!(install_dir(root, &changed), dir);
    let mut moved = pins.to_vec();
    moved[1].url = "https://example.com/elsewhere.tar.gz";
    assert_ne!(install_dir(root, &moved), dir);
}

#[test]
fn the_default_root_sits_beside_the_lite_data_dir() {
    let env = |name: &str| (name == "LOCALAPPDATA").then(|| OsString::from("L"));
    assert_eq!(
        default_root(embedded::Platform::Windows, &env, None),
        Some(
            PathBuf::from("L")
                .join("pseudolife-mcp")
                .join("native")
                .join("postgresql")
        )
    );
    let none = |_: &str| None;
    assert_eq!(default_root(embedded::Platform::Other, &none, None), None);
}

// ── provisioning ────────────────────────────────────────────────────────

#[test]
fn provisioning_installs_a_runtime_the_lifecycle_accepts() {
    let root = scratch("install");
    let (pins, fetch) = fake_set(cfg!(windows), true);
    let (runtime, installed) = provision(&root, pins, &fetch, &mut quiet()).unwrap();
    assert!(installed);
    let dir = install_dir(&root, pins);
    assert_eq!(runtime.root(), dir);
    for program in ["initdb", "pg_ctl", "postgres", "psql"] {
        assert_eq!(
            fs::read(dir.join("bin").join(exe(program))).unwrap(),
            b"binary"
        );
    }
    assert_eq!(fs::read(dir.join("share/README")).unwrap(), b"readme");
    // pgvector: the library into lib/, control and SQL into share/extension/,
    // nothing else (pg0's `install_pgvector`).
    assert_eq!(
        fs::read(dir.join("lib/vector.so")).unwrap(),
        b"vector library"
    );
    assert_eq!(
        fs::read(dir.join("share/extension/vector.control")).unwrap(),
        b"default_version = '0.8.5'"
    );
    assert!(dir.join("share/extension/vector--0.8.5.sql").is_file());
    assert!(!dir.join("include").exists());
    assert!(!dir.join("lib/bitcode").exists());
    assert!(!dir.join("lib/hnsw.bc").exists());
    // The deb: only the wanted library, into lib/.
    assert_eq!(
        fs::read(dir.join("lib/libxml2.so.2.9.13")).unwrap(),
        b"libxml2 bytes"
    );
    assert!(!dir.join("lib/copyright").exists());
    let provenance: serde_json::Value =
        serde_json::from_slice(&fs::read(dir.join(PROVENANCE)).unwrap()).unwrap();
    assert_eq!(provenance["postgresql"], "18.1.0");
    assert_eq!(provenance["pgvector"], "0.8.5");
    assert_eq!(provenance["artifacts"].as_array().unwrap().len(), 3);
    assert_eq!(provenance["artifacts"][2]["sha256"], pins[2].sha256);
    assert!(staging_left(&root).is_empty());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = |p: &str| {
            fs::symlink_metadata(dir.join(p))
                .unwrap()
                .permissions()
                .mode()
                & 0o777
        };
        assert_eq!(mode("bin/initdb"), 0o755);
        // Upstream modes 0666 and 0777 lose group and other write.
        assert_eq!(mode("share/README"), 0o644);
        assert_eq!(mode("share/extension/vector.control"), 0o755);
        assert_eq!(mode("lib/vector.so"), 0o755);
        assert_eq!(
            fs::read_link(dir.join("lib/libpq.so.5")).unwrap(),
            Path::new("libpq.so.5.18")
        );
        // The deb's SONAME link, made as pg0's `ensure_runtime_libs` does.
        assert_eq!(
            fs::read_link(dir.join("lib/libxml2.so.2")).unwrap(),
            Path::new("libxml2.so.2.9.13")
        );
    }
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn the_other_archive_format_installs_too() {
    // Windows pins a zip and the rest tar.gz; both paths run on every OS.
    let root = scratch("format");
    let (pins, fetch) = fake_set(!cfg!(windows), false);
    let (runtime, installed) = provision(&root, pins, &fetch, &mut quiet()).unwrap();
    assert!(installed);
    assert!(runtime.root().join("bin").join(exe("psql")).is_file());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_second_provision_finds_the_install_and_downloads_nothing() {
    let root = scratch("again");
    let (pins, fetch) = fake_set(cfg!(windows), true);
    provision(&root, pins, &fetch, &mut quiet()).unwrap();
    let calls = fetch.calls.borrow().len();
    assert_eq!(calls, 3);
    let mut lines = Vec::new();
    let (runtime, installed) =
        provision(&root, pins, &fetch, &mut |line| lines.push(line.to_owned())).unwrap();
    assert!(!installed);
    assert_eq!(runtime.root(), install_dir(&root, pins));
    assert_eq!(fetch.calls.borrow().len(), calls);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(lines[0].contains("already installed"), "{lines:?}");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn locate_answers_only_for_a_complete_matching_install() {
    let root = scratch("locate");
    let (pins, fetch) = fake_set(cfg!(windows), false);
    assert!(locate(&root, pins).unwrap().is_none());
    provision(&root, pins, &fetch, &mut quiet()).unwrap();
    let dir = install_dir(&root, pins);
    assert_eq!(locate(&root, pins).unwrap().unwrap().root(), dir);

    let provenance = dir.join(PROVENANCE);
    let original = fs::read(&provenance).unwrap();
    let edited = String::from_utf8(original.clone())
        .unwrap()
        .replace(pins[0].sha256, &"0".repeat(64));
    fs::write(&provenance, edited).unwrap();
    let error = locate(&root, pins).unwrap_err();
    assert!(matches!(error, Error::Installed { .. }), "{error}");
    let text = error.to_string();
    assert!(text.contains(&dir.display().to_string()), "{text}");
    assert!(text.contains("Move that directory aside"), "{text}");

    fs::write(&provenance, &original).unwrap();
    fs::remove_file(dir.join("bin").join(exe("psql"))).unwrap();
    let error = locate(&root, pins).unwrap_err();
    assert!(matches!(error, Error::Installed { .. }), "{error}");
    assert!(error.to_string().contains("psql"), "{error}");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_checksum_mismatch_installs_nothing() {
    let root = scratch("checksum");
    let (pins, mut fetch) = fake_set(cfg!(windows), true);
    fetch
        .files
        .insert(pins[1].url.to_owned(), b"not the pinned archive".to_vec());
    let error = provision(&root, pins, &fetch, &mut quiet()).unwrap_err();
    let Error::Checksum {
        url,
        expected,
        actual,
    } = &error
    else {
        panic!("{error}")
    };
    assert_eq!(url, pins[1].url);
    assert_eq!(expected, pins[1].sha256);
    assert_eq!(actual, &hex_sha256(b"not the pinned archive"));
    assert!(
        error.to_string().contains("nothing was installed"),
        "{error}"
    );
    assert!(!install_dir(&root, pins).exists());
    assert!(staging_left(&root).is_empty());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_failed_download_installs_nothing_and_names_the_url() {
    let root = scratch("download");
    let (pins, mut fetch) = fake_set(cfg!(windows), true);
    fetch.fail = Some(pins[2].url.to_owned());
    let error = provision(&root, pins, &fetch, &mut quiet()).unwrap_err();
    assert!(matches!(error, Error::Download { .. }), "{error}");
    let text = error.to_string();
    assert!(text.contains(pins[2].url), "{text}");
    assert!(text.contains("connection refused"), "{text}");
    assert!(text.contains("start again"), "{text}");
    assert!(!install_dir(&root, pins).exists());
    assert!(staging_left(&root).is_empty());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_staging_dir_left_by_a_killed_run_is_cleared() {
    let root = scratch("stale");
    let stale = root.join(".staging-0123");
    fs::create_dir_all(stale.join("tree/bin")).unwrap();
    fs::write(stale.join("download"), b"half").unwrap();
    let (pins, fetch) = fake_set(cfg!(windows), false);
    provision(&root, pins, &fetch, &mut quiet()).unwrap();
    assert!(!stale.exists());
    assert!(staging_left(&root).is_empty());
    fs::remove_dir_all(root).unwrap();
}

fn refused(archive: Vec<u8>, url: &str, unpack: Unpack, needle: &str) {
    let root = scratch("refused");
    let mut fetch = FakeFetch::default();
    let pins: &'static [Artifact] = Box::leak(
        vec![Artifact {
            url: leak(url.to_owned()),
            sha256: leak(hex_sha256(&archive)),
            unpack,
        }]
        .into_boxed_slice(),
    );
    fetch.files.insert(url.to_owned(), archive);
    let error = provision(&root, pins, &fetch, &mut quiet()).unwrap_err();
    assert!(matches!(error, Error::Archive { .. }), "{error}");
    let text = error.to_string();
    assert!(text.contains(needle), "{needle:?} not in {text}");
    assert!(text.contains("nothing was installed"), "{text}");
    assert!(!install_dir(&root, pins).exists());
    assert!(staging_left(&root).is_empty());
    assert!(!root.join("evil").exists());
    assert!(!root.parent().unwrap().join("evil").exists());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn archive_entries_that_leave_the_tree_are_refused() {
    let url = "https://example.com/pg.tar.gz";
    refused(
        tar_gz(&[Entry::File("top/../../evil", b"x", 0o644)]),
        url,
        Unpack::PostgreSql,
        "top/../../evil",
    );
    refused(
        tar_gz(&[Entry::File("/evil", b"x", 0o644)]),
        url,
        Unpack::PostgreSql,
        "/evil",
    );
    refused(
        zip_of(&[("top/../../evil", b"x")]),
        "https://example.com/pg.zip",
        Unpack::PostgreSql,
        "top/../../evil",
    );
    // A link may only name a file beside it.
    refused(
        tar_gz(&[Entry::Symlink("top/lib/libpq.so", "../../evil")]),
        url,
        Unpack::PostgreSql,
        "../../evil",
    );
    refused(
        tar_gz(&[Entry::Symlink("top/lib/libpq.so", "/etc/passwd")]),
        url,
        Unpack::PostgreSql,
        "/etc/passwd",
    );
}

#[test]
fn a_deb_without_a_wanted_library_or_zstd_data_is_refused() {
    let url = "https://example.com/lib.deb";
    refused(
        libxml2_deb(),
        url,
        Unpack::Deb(&["libicuuc.so.70.1"]),
        "libicuuc.so.70.1",
    );
    refused(
        deb_with("data.tar.xz", b"xz bytes".to_vec()),
        url,
        Unpack::Deb(LIBXML2),
        "data.tar.xz",
    );
    // A wanted name that is a link in the package, not the library itself.
    refused(
        libxml2_deb(),
        url,
        Unpack::Deb(&["libxml2.so.2"]),
        "libxml2.so.2",
    );
    refused(
        b"not an ar archive".to_vec(),
        url,
        Unpack::Deb(LIBXML2),
        "ar",
    );
}

#[test]
fn a_postgresql_archive_without_the_programs_is_refused_before_install() {
    let root = scratch("incomplete");
    let archive = tar_gz(&[Entry::File("top/bin/postgres", b"x", 0o755)]);
    let url = "https://example.com/pg.tar.gz";
    let mut fetch = FakeFetch::default();
    let pins: &'static [Artifact] = Box::leak(
        vec![Artifact {
            url,
            sha256: leak(hex_sha256(&archive)),
            unpack: Unpack::PostgreSql,
        }]
        .into_boxed_slice(),
    );
    fetch.files.insert(url.to_owned(), archive);
    let error = provision(&root, pins, &fetch, &mut quiet()).unwrap_err();
    assert!(matches!(error, Error::Incomplete { .. }), "{error}");
    let text = error.to_string();
    assert!(text.contains("initdb"), "{text}");
    assert!(text.contains("PostgreSQL 18.1.0 runtime"), "{text}");
    assert!(text.contains("nothing was installed"), "{text}");
    assert!(!install_dir(&root, pins).exists());
    assert!(staging_left(&root).is_empty());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn soname_follows_pg0() {
    assert_eq!(soname("libxml2.so.2.9.13").as_deref(), Some("libxml2.so.2"));
    assert_eq!(
        soname("libicudata.so.70.1").as_deref(),
        Some("libicudata.so.70")
    );
    assert_eq!(soname("libfoo.so"), None);
    assert_eq!(soname("libfoo.so."), None);
}

/// Downloads the real pins into `PSEUDOLIFE_TEST_PG_PROVISION_ROOT` (a
/// network test, run by hand on each platform's dev machine; the printed
/// directory is what `PSEUDOLIFE_TEST_PG_RUNTIME` then names).
#[test]
fn the_real_pins_download_verify_and_install() {
    let Some(root) = std::env::var_os("PSEUDOLIFE_TEST_PG_PROVISION_ROOT") else {
        return;
    };
    let root = PathBuf::from(root);
    let pins = current_artifacts().unwrap();
    let mut lines = Vec::new();
    let (runtime, _) = provision(&root, pins, &HttpFetch, &mut |line| {
        eprintln!("{line}");
        lines.push(line.to_owned());
    })
    .unwrap();
    eprintln!("runtime: {}", runtime.root().display());
    assert!(locate(&root, pins).unwrap().is_some());
    let output = std::process::Command::new(runtime.root().join("bin").join(exe("postgres")))
        .arg("--version")
        .envs(linux_loader_path(runtime.root()))
        .output()
        .unwrap();
    let version = String::from_utf8_lossy(&output.stdout);
    assert!(version.contains("18.1"), "{version}");
}

fn linux_loader_path(root: &Path) -> Vec<(String, OsString)> {
    if cfg!(target_os = "linux") {
        vec![("LD_LIBRARY_PATH".into(), root.join("lib").into_os_string())]
    } else {
        Vec::new()
    }
}
