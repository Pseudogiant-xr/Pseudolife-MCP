use std::{ffi::OsString, process::Command};

#[test]
fn invalid_unicode_mode_matches_python_repr_and_exit() {
    #[cfg(windows)]
    let (mode, repr) = {
        use std::os::windows::ffi::OsStringExt;
        (
            OsString::from_wide(&[0x62, 0x61, 0x64, 0xd800]),
            "'bad\\ud800'",
        )
    };
    #[cfg(unix)]
    let (mode, repr) = {
        use std::os::unix::ffi::OsStringExt;
        (OsString::from_vec(b"bad\xff".to_vec()), "'bad\\udcff'")
    };
    let output = Command::new(env!("CARGO_BIN_EXE_pseudolife-stdio"))
        .arg(mode)
        .env_clear()
        .env(
            "SystemRoot",
            std::env::var_os("SystemRoot").unwrap_or_default(),
        )
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let newline = if cfg!(windows) { "\r\n" } else { "\n" };
    assert_eq!(
        output.stderr,
        format!("unknown mode {repr}; see: pseudolife-mcp --help{newline}").as_bytes()
    );
}
