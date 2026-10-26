//! Pinned native runtimes, provisioned into `<per-user data dir>/native/`:
//! the shared core (`rust/shared/native_provision.rs`) and the PDFium
//! library PDF ingest loads.

use std::ffi::OsString;

// The daemon uses the tar path; the zip helpers serve other runtimes.
#[path = "../../../shared/native_provision.rs"]
#[allow(dead_code)]
pub mod native_provision;

pub mod pdf_text;
pub mod pdfium;

const USAGE: &str = "usage: pseudolife-daemon provision pdfium";

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
