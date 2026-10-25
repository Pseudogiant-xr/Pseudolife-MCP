//! Provisioning the lite tier's PostgreSQL runtime: the theseus-rs
//! PostgreSQL 18.1.0 archive, the pgvector 0.8.5 build and, on Linux, the
//! libxml2 and ICU 70 libraries that pg0 bundles beside them
//! (`rust/daemon/spec-lite-storage.md`, maintainer decisions 2, 3 and 5).
//!
//! pg0 embeds these archives at build time and extracts them to
//! `~/.pg0/installation/18.1.0`. This module downloads the same files,
//! checks each against the sha256 pinned below, and installs them into a
//! Pseudolife-owned directory laid out the way pg0 lays out its
//! installation, so `embedded` runs byte-identical binaries. Download,
//! verification, staging and the atomic install are `native_provision`'s.
//!
//! Deliberate differences from pg0: an archive entry that would leave the
//! install tree, a link to anything but a file beside it, or a hard link or
//! device is refused rather than skipped or followed; unpacked files lose
//! group and other write permission (pgvector_compiled ships mode 0777)
//! and otherwise keep their archive mode (pg0 sets everything in `bin/` and
//! `lib/` to 0755, static `.a` libraries included);
//! and the Ubuntu packages come from Launchpad's permanent file store at
//! the versions pg0 pins, because the Ubuntu pool deletes a package version
//! once a newer update supersedes it.

use super::embedded::{self, Runtime};
use super::native_provision::{self as native, Fetch, Manifest, Pinned};
use std::{
    collections::BTreeSet,
    ffi::OsString,
    fmt, fs,
    io::Read,
    path::{Path, PathBuf},
};

/// The PostgreSQL and pgvector versions Python lite's pinned pg0 runs.
pub const PG_VERSION: &str = "18.1.0";
pub const PGVECTOR_VERSION: &str = "0.8.5";
pub use native::{Error, HttpFetch, PROVENANCE};

/// How an artifact is laid into the install tree.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unpack {
    /// A theseus-rs archive (zip on Windows, tar.gz elsewhere): its one top
    /// directory becomes the install root.
    PostgreSql,
    /// A pgvector_compiled tar.gz: the library into `lib/`, the control and
    /// `vector--*` files into `share/extension/` (pg0's `install_pgvector`).
    Pgvector,
    /// An Ubuntu .deb: the named libraries from its `data.tar.zst` into
    /// `lib/`, each with its SONAME link (pg0's `ensure_runtime_libs`).
    Deb(&'static [&'static str]),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Artifact {
    pub url: &'static str,
    pub sha256: &'static str,
    pub unpack: Unpack,
}

const LIBXML2: &[&str] = &["libxml2.so.2.9.13"];
const ICU70: &[&str] = &[
    "libicudata.so.70.1",
    "libicui18n.so.70.1",
    "libicuuc.so.70.1",
];

// The pins. PostgreSQL: theseus-rs `postgresql-binaries` release 18.1.0
// (2025-11-17), the build pg0 0.15 bundles; pgvector: pg0's
// `PGVECTOR_COMPILED_TAG` v0.18.320; Linux libraries: pg0's `versions.env`
// (libxml2 2.9.13+dfsg-1ubuntu0.12 and libicu70 70.1-2ubuntu1, Ubuntu
// 22.04). Each sha256 was computed from a download on 2026-10-26 and agrees
// with GitHub's asset digest or pg0's `versions.env`; the Windows archives
// unpack to the same 3011 files, byte for byte, as pg0 0.15.1's installation.

const WINDOWS_X86_64: &[Artifact] = &[
    Artifact {
        url: "https://github.com/theseus-rs/postgresql-binaries/releases/download/18.1.0/postgresql-18.1.0-x86_64-pc-windows-msvc.zip",
        sha256: "ec0390b607f04e1499de7b0ec934f5bc4d999c4b5af02dafac3c6ad07e4744da",
        unpack: Unpack::PostgreSql,
    },
    Artifact {
        url: "https://github.com/nicoloboschi/pgvector_compiled/releases/download/v0.18.320/pgvector-x86_64-pc-windows-msvc-pg18.tar.gz",
        sha256: "2bfd570e6f4d7580c697a7db6cfb4db47d4936fbaa0c976f0f869f03cc40060f",
        unpack: Unpack::Pgvector,
    },
];

const LINUX_X86_64: &[Artifact] = &[
    Artifact {
        url: "https://github.com/theseus-rs/postgresql-binaries/releases/download/18.1.0/postgresql-18.1.0-x86_64-unknown-linux-gnu.tar.gz",
        sha256: "c8b2a68d26d0d766a5701a1c5ca7db1714ac88a47c3d4e82b5c2ad738d2e77df",
        unpack: Unpack::PostgreSql,
    },
    Artifact {
        url: "https://github.com/nicoloboschi/pgvector_compiled/releases/download/v0.18.320/pgvector-x86_64-unknown-linux-gnu-pg18.tar.gz",
        sha256: "2461d601bbf2c8ab3c70965be87dff92e5778d1610f9b294fcf23e32bd8a603e",
        unpack: Unpack::Pgvector,
    },
    Artifact {
        url: "https://launchpadlibrarian.net/865992006/libxml2_2.9.13+dfsg-1ubuntu0.12_amd64.deb",
        sha256: "b3678e6e4b166bc0e4226fb118d489ab51802c772914421397c9dcb2dd0e0d2b",
        unpack: Unpack::Deb(LIBXML2),
    },
    Artifact {
        url: "https://launchpadlibrarian.net/606433947/libicu70_70.1-2ubuntu1_amd64.deb",
        sha256: "223c1e6cebf2a6748637303227d8b84337c14a2d30eacbc46a68d02557dcff3e",
        unpack: Unpack::Deb(ICU70),
    },
];

const LINUX_AARCH64: &[Artifact] = &[
    Artifact {
        url: "https://github.com/theseus-rs/postgresql-binaries/releases/download/18.1.0/postgresql-18.1.0-aarch64-unknown-linux-gnu.tar.gz",
        sha256: "dd06150722d4f7d75dbbc93a1fd0faeb3a719943a8b4bf4d023d768476537278",
        unpack: Unpack::PostgreSql,
    },
    Artifact {
        url: "https://github.com/nicoloboschi/pgvector_compiled/releases/download/v0.18.320/pgvector-aarch64-unknown-linux-gnu-pg18.tar.gz",
        sha256: "c7aa638f11c1ccd219b4efc62de1fe5884b928f0088579f5d176f654c56832d6",
        unpack: Unpack::Pgvector,
    },
    Artifact {
        url: "https://launchpadlibrarian.net/865992050/libxml2_2.9.13+dfsg-1ubuntu0.12_arm64.deb",
        sha256: "6feb8502740e786e7d6a8d89e3d8d1060aaf6f27ad48bf279cb3c19b451eb16f",
        unpack: Unpack::Deb(LIBXML2),
    },
    Artifact {
        url: "https://launchpadlibrarian.net/606437105/libicu70_70.1-2ubuntu1_arm64.deb",
        sha256: "a06f86489e364f555a0655f476e6b2693760aff9d8dec0011daf9b17957da3c5",
        unpack: Unpack::Deb(ICU70),
    },
];

// macOS: the theseus binaries also need a host OpenSSL (pg0 issue #42).
const MACOS_AARCH64: &[Artifact] = &[
    Artifact {
        url: "https://github.com/theseus-rs/postgresql-binaries/releases/download/18.1.0/postgresql-18.1.0-aarch64-apple-darwin.tar.gz",
        sha256: "45e5401311ec80ecc2663c2d4752b2787f29acfcd084fd9d101c3097612a4b90",
        unpack: Unpack::PostgreSql,
    },
    Artifact {
        url: "https://github.com/nicoloboschi/pgvector_compiled/releases/download/v0.18.320/pgvector-aarch64-apple-darwin-pg18.tar.gz",
        sha256: "4c580ce63f2c7d462da9241b0d11f8f5038783de7a69a8aa3fe92d6996c75de6",
        unpack: Unpack::Pgvector,
    },
];

const MACOS_X86_64: &[Artifact] = &[
    Artifact {
        url: "https://github.com/theseus-rs/postgresql-binaries/releases/download/18.1.0/postgresql-18.1.0-x86_64-apple-darwin.tar.gz",
        sha256: "1acea5abab7c6d2269537026bb9e1ef5e8632ce24196ad7d84df75926903987d",
        unpack: Unpack::PostgreSql,
    },
    Artifact {
        url: "https://github.com/nicoloboschi/pgvector_compiled/releases/download/v0.18.320/pgvector-x86_64-apple-darwin-pg18.tar.gz",
        sha256: "e95a3a1f45e334b2bcbb070961f7ed9a33d118f8f99b36f91946f5c80e4f635c",
        unpack: Unpack::Pgvector,
    },
];

impl Pinned for Artifact {
    fn url(&self) -> &str {
        self.url
    }
    fn sha256(&self) -> &str {
        self.sha256
    }
}

/// A platform with no pinned build: the lite tier cannot run there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported {
    pub os: String,
    pub arch: String,
    pub env: String,
}
impl fmt::Display for Unsupported {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Self { os, arch, env } = self;
        write!(
            f,
            "the lite tier has no pinned PostgreSQL build for {os}/{arch} ({env}); run \
             against a PostgreSQL server instead by setting PSEUDOLIFE_MCP_DATABASE_URL."
        )
    }
}
impl std::error::Error for Unsupported {}

/// The pins for a platform (`std::env::consts::OS`/`ARCH` and the target
/// environment), or `None` where pg0 has no matching build.
pub fn artifacts_for(os: &str, arch: &str, env: &str) -> Option<&'static [Artifact]> {
    match (os, arch, env) {
        ("windows", "x86_64", "msvc") => Some(WINDOWS_X86_64),
        ("linux", "x86_64", "gnu") => Some(LINUX_X86_64),
        ("linux", "aarch64", "gnu") => Some(LINUX_AARCH64),
        ("macos", "aarch64", _) => Some(MACOS_AARCH64),
        ("macos", "x86_64", _) => Some(MACOS_X86_64),
        _ => None,
    }
}

/// The pins for the platform this binary was built for.
pub fn current_artifacts() -> Result<&'static [Artifact], Unsupported> {
    let env = if cfg!(target_env = "gnu") {
        "gnu"
    } else if cfg!(target_env = "msvc") {
        "msvc"
    } else if cfg!(target_env = "musl") {
        "musl"
    } else {
        ""
    };
    let (os, arch) = (std::env::consts::OS, std::env::consts::ARCH);
    artifacts_for(os, arch, env).ok_or_else(|| Unsupported {
        os: os.into(),
        arch: arch.into(),
        env: env.into(),
    })
}

/// The per-user directory holding installed runtimes,
/// `<default lite data dir>/native/postgresql`, whatever data dir a bank
/// uses (other pinned native runtimes sit beside it under `native/`).
pub fn default_root(
    platform: embedded::Platform,
    env: &dyn Fn(&str) -> Option<OsString>,
    home: Option<&Path>,
) -> Option<PathBuf> {
    embedded::default_data_dir(platform, env, home).map(|dir| dir.join("native").join("postgresql"))
}

const LABEL: &str = "PostgreSQL 18.1.0 runtime";
/// Raised when the unpack layout changes, so trees laid out the old way are
/// installed again beside, not trusted.
const LAYOUT: u32 = 1;

fn manifest(artifacts: &[Artifact]) -> Manifest<'_, Artifact> {
    let mut record = serde_json::Map::new();
    record.insert("postgresql".into(), PG_VERSION.into());
    record.insert("pgvector".into(), PGVECTOR_VERSION.into());
    record.insert("layout".into(), LAYOUT.into());
    Manifest {
        label: LABEL,
        version: PG_VERSION,
        artifacts,
        record,
    }
}

/// One directory per pin set, so a changed pin installs beside the old
/// runtime instead of over a tree a running server is using.
pub fn install_dir(root: &Path, artifacts: &[Artifact]) -> PathBuf {
    manifest(artifacts).install_dir(root)
}

/// The installed runtime for these pins: `None` when there is none, an
/// error when the directory exists but is not a complete install of them.
pub fn locate(root: &Path, artifacts: &[Artifact]) -> Result<Option<Runtime>, Error> {
    native::locate(root, &manifest(artifacts), &|dir| validate(dir, artifacts))
}

/// Installs the runtime for these pins under `root` unless it is already
/// there; `true` when this call installed it.
pub fn provision(
    root: &Path,
    artifacts: &[Artifact],
    fetch: &dyn Fetch,
    log: &mut dyn FnMut(&str),
) -> Result<(Runtime, bool), Error> {
    native::provision(
        root,
        &manifest(artifacts),
        fetch,
        log,
        &mut unpack,
        &|dir| validate(dir, artifacts),
    )
}

/// What the lifecycle needs (`Runtime::new`: the programs and pgvector's
/// control file), plus pgvector's library and each bundled library with
/// its SONAME link.
fn validate(dir: &Path, artifacts: &[Artifact]) -> Result<Runtime, String> {
    let runtime = Runtime::new(dir.to_path_buf()).map_err(|error| match error {
        embedded::Error::Runtime { missing, .. } => format!("{missing} is missing"),
        other => other.to_string(),
    })?;
    let lib = dir.join("lib");
    if !["vector.so", "vector.dylib", "vector.dll"]
        .iter()
        .any(|name| lib.join(name).is_file())
    {
        return Err("lib/vector (pgvector's library) is missing".into());
    }
    for artifact in artifacts {
        let Unpack::Deb(wanted) = artifact.unpack else {
            continue;
        };
        for name in wanted {
            if !lib.join(name).is_file() {
                return Err(format!("lib/{name} is missing"));
            }
            #[cfg(unix)]
            if let Some(link) = soname(name)
                && fs::symlink_metadata(lib.join(&link)).is_err()
            {
                return Err(format!("lib/{link} is missing"));
            }
        }
    }
    Ok(runtime)
}

fn unpack(artifact: &Artifact, download: &Path, tree: &Path) -> Result<(), String> {
    match artifact.unpack {
        Unpack::PostgreSql if artifact.url.ends_with(".zip") => {
            native::unpack_zip_tree(download, tree, native::Layout::strip_top())
        }
        Unpack::PostgreSql => {
            native::unpack_tar_tree(native::gzip(download)?, tree, native::Layout::strip_top())
        }
        Unpack::Pgvector => unpack_pgvector(native::gzip(download)?, tree),
        Unpack::Deb(wanted) => unpack_deb(download, tree, wanted),
    }
}

/// pg0's `install_pgvector`: the library into `lib/`, `vector.control` and
/// the `vector--*` scripts into `share/extension/`, everything else skipped.
fn unpack_pgvector(reader: impl Read, tree: &Path) -> Result<(), String> {
    let mut archive = tar::Archive::new(reader);
    for entry in archive.entries().map_err(|e| e.to_string())? {
        let mut entry = entry.map_err(|e| e.to_string())?;
        let raw = String::from_utf8_lossy(&entry.path_bytes()).into_owned();
        match entry.header().entry_type() {
            tar::EntryType::Directory => continue,
            tar::EntryType::Regular | tar::EntryType::Continuous => {}
            other => {
                return Err(format!(
                    "entry {raw:?} is a {other:?}, which the pgvector archives never hold"
                ));
            }
        }
        let name = native::file_name(&raw)?;
        let dest = if name.ends_with(".so") || name.ends_with(".dylib") || name.ends_with(".dll") {
            tree.join("lib").join(name)
        } else if name == "vector.control" || name.starts_with("vector--") {
            tree.join("share").join("extension").join(name)
        } else {
            continue;
        };
        let mode = entry.header().mode().map_err(|e| e.to_string())?;
        native::write_new(&dest, &mut entry, mode).map_err(|e| format!("{raw}: {e}"))?;
    }
    Ok(())
}

/// The `data.tar*` member of a .deb (an `ar` archive): its name and bytes.
fn deb_data(bytes: &[u8]) -> Result<(String, &[u8]), String> {
    let mut rest = bytes
        .strip_prefix(b"!<arch>\n")
        .ok_or("it is not an ar archive (a .deb)")?;
    while rest.len() >= 60 {
        let (header, body) = rest.split_at(60);
        if &header[58..60] != b"`\n" {
            return Err("an ar member header is malformed".into());
        }
        let name = String::from_utf8_lossy(&header[..16])
            .trim_end()
            .trim_end_matches('/')
            .to_owned();
        let size: usize = std::str::from_utf8(&header[48..58])
            .ok()
            .and_then(|s| s.trim().parse().ok())
            .ok_or("an ar member size is unreadable")?;
        if size > body.len() {
            return Err(format!("ar member {name:?} runs past the end"));
        }
        if name.starts_with("data.tar") {
            return Ok((name, &body[..size]));
        }
        rest = &body[(size + (size & 1)).min(body.len())..];
    }
    Err("it holds no data.tar member".into())
}

/// pg0's `ensure_runtime_libs`: the wanted libraries from a package into
/// `lib/`, each with its SONAME link.
fn unpack_deb(path: &Path, tree: &Path, wanted: &[&str]) -> Result<(), String> {
    let bytes = fs::read(path).map_err(|e| e.to_string())?;
    let (member, data) = deb_data(&bytes)?;
    if member != "data.tar.zst" {
        return Err(format!(
            "its data member is {member}; only data.tar.zst (zstd) is supported"
        ));
    }
    let decoder = ruzstd::decoding::StreamingDecoder::new(data).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(decoder);
    let lib = tree.join("lib");
    let mut found = BTreeSet::new();
    for entry in archive.entries().map_err(|e| e.to_string())? {
        let mut entry = entry.map_err(|e| e.to_string())?;
        if entry.header().entry_type() == tar::EntryType::Directory {
            continue;
        }
        let raw = String::from_utf8_lossy(&entry.path_bytes()).into_owned();
        let name = native::file_name(&raw)?.to_owned();
        if !wanted.contains(&name.as_str()) {
            continue;
        }
        if !matches!(
            entry.header().entry_type(),
            tar::EntryType::Regular | tar::EntryType::Continuous
        ) {
            return Err(format!("{name} is a link in the package, not the library"));
        }
        native::write_new(&lib.join(&name), &mut entry, 0o755)
            .map_err(|e| format!("{raw}: {e}"))?;
        found.insert(name);
    }
    if let Some(missing) = wanted.iter().find(|w| !found.contains(**w)) {
        return Err(format!("{missing} is not in the package"));
    }
    #[cfg(unix)]
    for name in wanted {
        if let Some(link) = soname(name) {
            let link = lib.join(link);
            if fs::symlink_metadata(&link).is_err() {
                std::os::unix::fs::symlink(name, &link).map_err(|e| e.to_string())?;
            }
        }
    }
    Ok(())
}

/// The name the dynamic loader looks a library up by: `libxml2.so.2` for
/// `libxml2.so.2.9.13` (pg0's `soname_for`).
#[cfg_attr(not(unix), allow(dead_code))]
fn soname(filename: &str) -> Option<String> {
    let index = filename.find(".so.")?;
    let (stem, rest) = filename.split_at(index + ".so.".len());
    let major = rest.split('.').next()?;
    (!major.is_empty()).then(|| format!("{stem}{major}"))
}

#[cfg(test)]
#[path = "pg_runtime_tests.rs"]
mod tests;
