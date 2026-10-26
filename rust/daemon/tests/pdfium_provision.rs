//! The PDFium provisioner (`src/native/pdfium.rs` on the shared
//! `native_provision` core) with its tests, in a test binary of its own so
//! the hosted stress loop (`rust/ci/stress.py`) can select them: an install
//! publishes with a directory rename, which behaves differently on
//! `windows-latest` than on a dev machine.

#[path = "../../shared/native_provision.rs"]
#[allow(dead_code)]
mod native_provision;

#[path = "../src/native/pdfium.rs"]
#[allow(dead_code, unused_imports)]
mod pdfium;
