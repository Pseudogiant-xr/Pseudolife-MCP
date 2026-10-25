//! Pinned native runtimes, provisioned into `<per-user data dir>/native/`:
//! the shared core (`rust/shared/native_provision.rs`) and the PDFium
//! library PDF ingest loads.

use std::ffi::OsString;

// The daemon uses the tar path; the zip helpers serve other runtimes.
#[path = "../../../shared/native_provision.rs"]
#[allow(dead_code)]
pub mod native_provision;

pub mod pdfium;

// The lite tier: the pinned PostgreSQL runtime, the embedded server's
// lifecycle, the bank's password file and (on Windows) the owner-only file
// code they rest on. The shim includes the same files; the daemon calls a
// part of each.
#[path = "../../../shared/embedded_pg.rs"]
#[allow(dead_code)]
pub mod embedded;
pub mod lite;
#[path = "../../../shared/pg_runtime.rs"]
#[allow(dead_code, unused_imports)]
pub mod pg_runtime;
#[path = "../../../shared/secret_file.rs"]
#[allow(dead_code)]
pub mod secret_file;
#[cfg(windows)]
#[allow(unsafe_code, dead_code)]
#[path = "../../../shared/windows_security.rs"]
pub(crate) mod windows_security;

/// The error the owner-only file code reports (the shim's
/// `credentials::CredentialError`).
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CredentialError(pub &'static str);

const USAGE: &str = "usage: pseudolife-daemon provision pdfium|postgresql";

/// `pseudolife-daemon provision <runtime>`: installs a pinned native
/// runtime and exits; it needs no bank, configuration or token. Returns the
/// exit code: 0 installed or already there, 1 failed (nothing installed),
/// 2 a usage error.
pub fn command(args: &[OsString]) -> i32 {
    let runtime = match args {
        [one] => one.to_string_lossy(),
        _ => {
            eprintln!("{USAGE}");
            return 2;
        }
    };
    match runtime.as_ref() {
        "pdfium" => provision_pdfium(),
        "postgresql" => lite::provision_command(),
        other => {
            eprintln!("pseudolife-daemon provision: no runtime named {other:?}; {USAGE}");
            2
        }
    }
}

fn provision_pdfium() -> i32 {
    let build = match pdfium::Build::current() {
        Ok(build) => build,
        Err(e) => {
            eprintln!("pseudolife-daemon provision pdfium: {e}; nothing was installed.");
            return 1;
        }
    };
    let Some(root) = pdfium::root() else {
        eprintln!(
            "pseudolife-daemon provision pdfium: no install directory is known; set {} to \
             the directory to install into. Nothing was installed.",
            pdfium::ROOT_ENV
        );
        return 1;
    };
    match build.provision(&root, &pdfium::HttpFetch, &mut |line| println!("{line}")) {
        Ok(_) => 0,
        Err(e) => {
            eprintln!("pseudolife-daemon provision pdfium: {e}");
            1
        }
    }
}
