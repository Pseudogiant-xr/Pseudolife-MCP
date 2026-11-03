use std::{env, path::PathBuf};

mod build_support;

fn main() {
    let compiler = env::var_os("RUSTC").expect("Cargo supplies the resolved compiler");
    let target = env::var("TARGET").expect("Cargo supplies the target triple");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let linker = env::var_os("RUSTC_LINKER");
    let encoded_rustflags = env::var("CARGO_ENCODED_RUSTFLAGS").ok();
    println!("cargo:rerun-if-changed=build_support.rs");
    println!("cargo:rerun-if-env-changed=RUSTC_LINKER");
    println!("cargo:rerun-if-env-changed=CARGO_ENCODED_RUSTFLAGS");
    for name in ["stub", "launcher"] {
        let source = PathBuf::from("..").join(format!("{name}.rs"));
        println!("cargo:rerun-if-changed={}", source.display());
        let mut command = build_support::fixture_command(
            &compiler,
            &target,
            &source,
            &out.join(format!("{name}.bin")),
            linker.as_deref(),
            encoded_rustflags.as_deref(),
        );
        assert!(command.status().expect("compile test fixture").success());
    }
}
