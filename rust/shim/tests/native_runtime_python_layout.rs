//! A native install over the layout a Python client left (the Python-to-native
//! update every existing user takes) keeps that client's files usable.
//!
//! On Windows the Python client's runtimes root and launcher folder inherit
//! their access from the user's profile, and every file in them has only
//! inherited entries. Protecting those folders with an access list that has
//! no inheritable entry left each of those files with an empty one: the old
//! client could no longer be read or run, and the native install failed with
//! "Access is denied".
#![allow(dead_code)]
mod native_binary_fixture;
use pseudolife_stdio::runtime::{self, BINARY, Layout};
use std::{fs, path::PathBuf};

/// A home whose folders inherit their access, as a user profile's do. (A
/// home made with `private_dir` would give everything under it explicit
/// entries and hide the defect.)
struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "pseudolife-python-layout-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&path).unwrap();
        // The profile's own entries (SYSTEM, Administrators, this account),
        // inherited by everything below: %TEMP% itself can carry more (the
        // Codex sandbox's accounts may modify it), and the folder rule would
        // then rightly refuse it.
        #[cfg(windows)]
        {
            let me = std::process::Command::new("whoami").output().unwrap();
            let me = String::from_utf8_lossy(&me.stdout).trim().to_owned();
            let status = std::process::Command::new("icacls")
                .arg(&path)
                .args([
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F",
                ])
                .arg(format!("{me}:(OI)(CI)F"))
                .arg("/Q")
                .stdout(std::process::Stdio::null())
                .status()
                .unwrap();
            assert!(status.success(), "icacls on {}", path.display());
        }
        Self(path)
    }
    fn layout(&self) -> Layout {
        let local = self.0.join("AppData").join("Local").join("pseudolife-mcp");
        runtime::layout_from(|key| match key {
            "PSEUDOLIFE_SHIM_RUNTIMES" => Some(local.join("runtimes")),
            "PSEUDOLIFE_SHIM_LAUNCHER" => Some(local.join("bin").join(BINARY)),
            _ => None,
        })
        .unwrap()
    }
    fn command(&self, program: &std::path::Path) -> std::process::Command {
        let mut command = std::process::Command::new(program);
        let local = self.0.join("AppData").join("Local").join("pseudolife-mcp");
        command
            .env("PSEUDOLIFE_SHIM_RUNTIMES", local.join("runtimes"))
            .env("PSEUDOLIFE_SHIM_LAUNCHER", local.join("bin").join(BINARY));
        command
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// What the Python client leaves: a numbered venv with its marker and console
/// script, and its launcher, all written with ordinary (inherited) access.
fn python_layout(layout: &Layout) -> Vec<PathBuf> {
    let venv = layout.root.join("000001");
    let scripts = venv.join(if cfg!(windows) { "Scripts" } else { "bin" });
    fs::create_dir_all(&scripts).unwrap();
    fs::create_dir_all(layout.launcher.parent().unwrap()).unwrap();
    let files = vec![
        venv.join("runtime.json"),
        scripts.join(BINARY),
        venv.join("pyvenv.cfg"),
        // The Python client's console-script launcher, which the native
        // install replaces after keeping a backup.
        layout.launcher.clone(),
    ];
    fs::write(
        &files[0],
        br#"{"version":"0.23.1","source":"pseudolife-mcp==0.23.1"}"#,
    )
    .unwrap();
    fs::write(&files[1], b"python console script").unwrap();
    fs::write(&files[2], b"home = C:\\Python311\n").unwrap();
    fs::write(&files[3], b"python launcher").unwrap();
    files
}

#[cfg(windows)]
fn release_archive(binary: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    writer
        .start_file(
            BINARY,
            zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated)
                .unix_permissions(0o755),
        )
        .unwrap();
    writer.write_all(binary).unwrap();
    writer.finish().unwrap().into_inner()
}
#[cfg(unix)]
fn release_archive(binary: &[u8]) -> Vec<u8> {
    let encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    let mut writer = tar::Builder::new(encoder);
    let mut header = tar::Header::new_gnu();
    header.set_size(binary.len() as u64);
    header.set_mode(0o755);
    header.set_cksum();
    writer.append_data(&mut header, BINARY, binary).unwrap();
    writer.into_inner().unwrap().finish().unwrap()
}

#[test]
fn a_native_install_over_a_python_client_keeps_its_files_readable() {
    let home = Home::new();
    let layout = home.layout();
    let python = python_layout(&layout);
    let candidate = native_binary_fixture::installable(
        std::path::Path::new(env!("CARGO_BIN_EXE_pseudolife-stdio")),
        &home.0,
    );
    let binary = fs::read(&candidate).unwrap();
    let archive = release_archive(&binary);
    use sha2::{Digest, Sha256};
    let name = format!(
        "pseudolife-mcp-1.0.0-{}.{}",
        runtime::target_triple().unwrap(),
        if cfg!(windows) { "zip" } else { "tar.gz" }
    );
    let sums = format!("{:x}  {name}\n", Sha256::digest(&archive));
    {
        // The updater takes the lock first, as `runtime install` does.
        let _lock = runtime::UpdateLock::acquire(&layout).unwrap();
        runtime::install_archive(&layout, "1.0.0", "pseudolife-mcp==1.0.0", &archive, &sums)
            .unwrap_or_else(|error| panic!("native install over a Python client: {error}"));
    }
    // The Python client is still there, readable and replaceable by its owner.
    // (The launcher, the fourth, was replaced by the native one.)
    for path in &python[..3] {
        fs::read(path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    }
    fs::write(layout.root.join("000001").join("pyvenv.cfg"), b"rewritten").unwrap();
    // And the native launcher that replaced the Python one runs.
    let result = home
        .command(&layout.launcher)
        .arg("--help")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
}
