//! The PDFium library PDF ingest loads. `pdfium-pin.json` (written by
//! `pin_pdfium.py`, embedded at compile time) pins one
//! `bblanchon/pdfium-binaries` release per target: the archive's sha256 and
//! the library's inside it. `pseudolife-daemon provision pdfium` installs
//! that archive's library, its LICENSE and its `licenses/` into
//! `<root>/<build>-<12 hex>/` through the shared provisioner; the daemon
//! itself never downloads it. PDF ingest loads only a library whose recorded
//! pins and own sha256 match this build.
//!
//! The pin moves together with the Python oracle's `pypdfium2`
//! (`ops/requirements.lock.txt`), whose wheels bundle the same builds.

use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use super::native_provision::{
    self as native, Fetch, Manifest, Pinned, Platform, default_data_dir,
};

pub use native::{Error, HttpFetch};

/// Names the install root in place of the per-user default (the container
/// image sets it to `/opt/pdfium`).
pub const ROOT_ENV: &str = "PSEUDOLIFE_DAEMON_PDFIUM_DIR";

/// One target's archive and the library inside it.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct TargetPin {
    pub url: String,
    pub archive_sha256: String,
    pub library: String,
    pub library_sha256: String,
}

/// The whole pin file.
#[derive(Clone, Debug)]
pub struct Pins {
    pub tag: String,
    pub build: String,
    pub targets: BTreeMap<String, TargetPin>,
}

#[derive(serde::Deserialize)]
struct PinFile {
    tag: String,
    targets: BTreeMap<String, TargetPin>,
}

const PIN_JSON: &str = include_str!("../../pdfium-pin.json");

impl Pins {
    pub fn target(&self, name: &str) -> Option<&TargetPin> {
        self.targets.get(name)
    }
}

pub fn all_pins() -> Pins {
    let file: PinFile = serde_json::from_str(PIN_JSON).expect("pdfium-pin.json parses");
    let build = file.tag.rsplit('/').next().unwrap_or_default().to_string();
    Pins {
        tag: file.tag,
        build,
        targets: file.targets,
    }
}

/// The pin-file key for the platform this daemon was built for: the
/// library must match the process that loads it.
pub fn this_target() -> Option<&'static str> {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    Some(match (os, arch) {
        ("windows", "x86_64") => "windows-x86_64",
        ("windows", "aarch64") => "windows-aarch64",
        ("linux", "x86_64") if cfg!(target_env = "musl") => "linux-x86_64-musl",
        ("linux", "x86_64") if cfg!(target_env = "gnu") => "linux-x86_64-gnu",
        ("linux", "aarch64") if cfg!(target_env = "gnu") => "linux-aarch64-gnu",
        ("macos", "aarch64") => "macos-aarch64",
        ("macos", "x86_64") => "macos-x86_64",
        _ => return None,
    })
}

/// This build's platform as a refusal names it, with the C library on
/// Linux (a musl build cannot load the glibc library).
fn platform_label() -> String {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    let libc = if os != "linux" {
        ""
    } else if cfg!(target_env = "musl") {
        "-musl"
    } else if cfg!(target_env = "gnu") {
        "-gnu"
    } else {
        "-other-libc"
    };
    format!("{os}-{arch}{libc}")
}

/// What one install is built from: the release tag and this target's pin.
#[derive(Clone, Debug)]
pub struct Build {
    pub tag: String,
    pub build: String,
    pub pin: TargetPin,
}

impl Build {
    /// This daemon's build, or why there is none for this platform.
    pub fn current() -> Result<Self, String> {
        let pins = all_pins();
        let target = this_target()
            .ok_or_else(|| format!("no PDFium build is pinned for {}", platform_label()))?;
        let pin = pins
            .target(target)
            .cloned()
            .ok_or_else(|| format!("pdfium-pin.json has no {target} entry"))?;
        Ok(Self {
            tag: pins.tag,
            build: pins.build,
            pin,
        })
    }

    /// The library's file name in the install directory.
    pub fn library_name(&self) -> &str {
        self.pin.library.rsplit('/').next().unwrap_or_default()
    }

    fn label(&self) -> String {
        format!("PDFium {} library", self.tag)
    }

    fn with_manifest<R>(&self, f: impl FnOnce(&Manifest<'_, Archive<'_>>) -> R) -> R {
        let label = self.label();
        let archives = [Archive(&self.pin)];
        let mut record = serde_json::Map::new();
        // Raised whenever `unpack` lays the tree out differently, which
        // moves the install to a new directory.
        record.insert("layout".into(), UNPACK_LAYOUT.into());
        record.insert("tag".into(), self.tag.clone().into());
        record.insert("library".into(), self.pin.library.clone().into());
        record.insert(
            "library_sha256".into(),
            self.pin.library_sha256.clone().into(),
        );
        f(&Manifest {
            label: &label,
            version: &self.build,
            artifacts: &archives,
            record,
        })
    }

    /// `<root>/<build>-<12 hex of its provenance record>`: the pins and the
    /// unpack layout, so a change to either installs beside the old one.
    pub fn install_dir(&self, root: &Path) -> PathBuf {
        self.with_manifest(|m| m.install_dir(root))
    }

    /// The installed library, `None` when nothing is installed, or an
    /// error when an install is there but does not match the pins.
    pub fn locate(&self, root: &Path) -> Result<Option<PathBuf>, Error> {
        self.with_manifest(|m| native::locate(root, m, &|dir| self.validate(dir)))
    }

    /// Installs this build's library under `root` unless it is there;
    /// `true` when this call installed it.
    pub fn provision(
        &self,
        root: &Path,
        fetch: &dyn Fetch,
        log: &mut dyn FnMut(&str),
    ) -> Result<(PathBuf, bool), Error> {
        self.with_manifest(|m| {
            native::provision(
                root,
                m,
                fetch,
                log,
                &mut |_, download, tree| self.unpack(download, tree),
                &|dir| self.validate(dir),
            )
        })
    }

    /// Lays the library, `LICENSE` and the files under `licenses/` into
    /// `tree`; every other entry (headers, CMake files) is skipped.
    fn unpack(&self, download: &Path, tree: &Path) -> Result<(), String> {
        let mut archive = tar::Archive::new(native::gzip(download)?);
        for entry in archive.entries().map_err(|e| e.to_string())? {
            let mut entry = entry.map_err(|e| e.to_string())?;
            let raw = String::from_utf8_lossy(&entry.path_bytes()).into_owned();
            let Some(dest) = self.destination(&raw) else {
                continue;
            };
            if !matches!(
                entry.header().entry_type(),
                tar::EntryType::Regular | tar::EntryType::Continuous
            ) {
                return Err(format!("entry {raw:?} is not a regular file"));
            }
            let mode = entry.header().mode().map_err(|e| e.to_string())?;
            let dest = tree.join(dest);
            // `licenses/` gets the core's directory mode, not the umask's.
            if let Some(parent) = dest.parent().filter(|p| *p != tree) {
                native::create_dir(parent).map_err(|e| format!("{raw}: {e}"))?;
            }
            native::write_new(&dest, &mut entry, mode).map_err(|e| format!("{raw}: {e}"))?;
        }
        Ok(())
    }

    /// Where an archive entry goes in the tree, if it is kept.
    fn destination(&self, raw: &str) -> Option<PathBuf> {
        let raw = raw.strip_prefix("./").unwrap_or(raw);
        if raw == self.pin.library {
            return Some(PathBuf::from(self.library_name()));
        }
        if raw == "LICENSE" {
            return Some(PathBuf::from("LICENSE"));
        }
        let name = raw.strip_prefix("licenses/")?;
        let plain = !name.is_empty() && !name.starts_with('.') && !name.contains(['/', '\\', ':']);
        plain.then(|| Path::new("licenses").join(name))
    }

    /// The tree holds this build's library, byte for byte.
    fn validate(&self, dir: &Path) -> Result<PathBuf, String> {
        let path = dir.join(self.library_name());
        let actual = sha256_file(&path)
            .map_err(|e| format!("its {} cannot be read: {e}", self.library_name()))?;
        if actual != self.pin.library_sha256 {
            return Err(format!(
                "its {} has sha256 {actual}, not the pinned {}",
                self.library_name(),
                self.pin.library_sha256
            ));
        }
        Ok(path)
    }
}

/// The version of the tree `unpack` lays out (library, LICENSE, licenses/).
const UNPACK_LAYOUT: u64 = 1;

struct Archive<'a>(&'a TargetPin);

impl Pinned for Archive<'_> {
    fn url(&self) -> &str {
        &self.0.url
    }
    fn sha256(&self) -> &str {
        &self.0.archive_sha256
    }
}

fn sha256_file(path: &Path) -> std::io::Result<String> {
    use sha2::{Digest, Sha256};
    let mut file = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    Ok(native::hex(&hash.finalize()))
}

/// The install root: `PSEUDOLIFE_DAEMON_PDFIUM_DIR` when set, else
/// `<per-user data dir>/native/pdfium` (beside the lite tier's
/// `native/postgresql`), whatever data dir a bank uses.
pub fn default_root(
    platform: Platform,
    env: &dyn Fn(&str) -> Option<std::ffi::OsString>,
    home: Option<&Path>,
) -> Option<PathBuf> {
    if let Some(dir) = env(ROOT_ENV).filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(dir));
    }
    default_data_dir(platform, env, home).map(|dir| dir.join("native").join("pdfium"))
}

/// `default_root` for this process.
pub fn root() -> Option<PathBuf> {
    default_root(
        Platform::current(),
        &|name| std::env::var_os(name),
        std::env::home_dir().as_deref(),
    )
}

const PROVISION: &str = "`pseudolife-daemon provision pdfium`";

/// The refusal PDF ingest answers when there is no usable library: the
/// cause, then the one step that fixes it, if any; text and HTML ingest
/// never need the library.
fn unavailable(cause: &str, fix: Option<&str>) -> String {
    let fix = fix
        .map(|f| format!(": {f}, then ingest the file again"))
        .unwrap_or_default();
    format!(
        "pdf_engine_unavailable: reading a PDF needs the PDFium library, and {cause}{fix}; \
         text, Markdown and HTML files ingest without it"
    )
}

/// The installed library for PDF ingest, or the `pdf_engine_unavailable`
/// refusal naming why there is none and what fixes it.
#[allow(dead_code)] // PDF ingest calls it from the extraction PR.
pub fn library() -> Result<PathBuf, String> {
    library_at(Build::current(), root())
}

fn library_at(build: Result<Build, String>, root: Option<PathBuf>) -> Result<PathBuf, String> {
    let build = build.map_err(|cause| unavailable(&cause, None))?;
    let Some(root) = root else {
        return Err(unavailable(
            "no install directory is known (no LOCALAPPDATA, XDG_DATA_HOME or home directory)",
            Some(&format!("set {ROOT_ENV} to one and run {PROVISION}")),
        ));
    };
    match build.locate(&root) {
        Ok(Some(path)) => Ok(path),
        Ok(None) => Err(unavailable(
            &format!(
                "it is not installed at {}",
                build.install_dir(&root).display()
            ),
            Some(&format!("run {PROVISION}")),
        )),
        Err(Error::Installed { dir, detail }) => Err(unavailable(
            &format!(
                "the install at {} does not match this build ({detail})",
                dir.display()
            ),
            Some(&format!("move that directory aside and run {PROVISION}")),
        )),
        Err(e) => Err(unavailable(&e.to_string(), None)),
    }
}

#[cfg(test)]
#[path = "pdfium_tests.rs"]
mod tests;
