use std::{env, path::PathBuf, process::Command};

fn main() {
    let compiler = env::var_os("RUSTC").expect("Cargo supplies the resolved compiler");
    let target = env::var("TARGET").expect("Cargo supplies the target triple");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    for name in ["stub", "launcher"] {
        let source = PathBuf::from("..").join(format!("{name}.rs"));
        println!("cargo:rerun-if-changed={}", source.display());
        let mut command = Command::new(&compiler);
        command
            .arg("--edition=2024")
            .arg("--target")
            .arg(&target)
            .arg(&source)
            .arg("-o")
            .arg(out.join(format!("{name}.bin")));
        if let Some(linker) = env::var_os("RUSTC_LINKER") {
            let mut option = std::ffi::OsString::from("linker=");
            option.push(linker);
            command.arg("-C").arg(option);
        }
        assert!(command.status().expect("compile test fixture").success());
    }
}
