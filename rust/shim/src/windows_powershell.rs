//! Windows PowerShell invocation shared by the scheduler registration paths.
use std::path::PathBuf;

pub(crate) fn executable() -> Result<String, String> {
    let root = std::env::var_os("SystemRoot").map(PathBuf::from).ok_or(
        "SystemRoot is missing; cannot locate Windows PowerShell for the scheduled environment",
    )?;
    if !root.is_absolute() {
        return Err("SystemRoot must be an absolute Windows directory; restore SystemRoot before retrying registration".into());
    }
    root.join("System32/WindowsPowerShell/v1.0/powershell.exe")
        .to_str()
        .map(str::to_owned)
        .ok_or_else(|| "the scheduler executable path must be UTF-8".into())
}

pub(crate) fn encoded(script: &str) -> Result<Vec<String>, String> {
    Ok(vec![
        executable()?,
        "-NoLogo".into(),
        "-NoProfile".into(),
        "-NonInteractive".into(),
        "-EncodedCommand".into(),
        base64(
            &script
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        ),
    ])
}

pub(crate) fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let a = chunk[0] as usize;
        let b = chunk.get(1).copied().unwrap_or(0) as usize;
        let c = chunk.get(2).copied().unwrap_or(0) as usize;
        out.push(ALPHABET[a >> 2] as char);
        out.push(ALPHABET[((a & 3) << 4) | (b >> 4)] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[((b & 15) << 2) | (c >> 6)] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[c & 63] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    fn check_interpreter_root(planted: Option<&str>) {
        let expected = PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        let mut command = std::process::Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "windows_powershell::tests::interpreter_path_probe",
                "--ignored",
                "--nocapture",
            ])
            .env("PSEUDOLIFE_TEST_EXPECTED_POWERSHELL", expected);
        if let Some(root) = planted {
            command.env("SystemRoot", root);
        }
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "{} {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    #[ignore = "subprocess probe, invoked by the three interpreter-root tests"]
    fn interpreter_path_probe() {
        let expected = std::env::var("PSEUDOLIFE_TEST_EXPECTED_POWERSHELL").unwrap();
        assert_eq!(executable().unwrap(), expected);
    }

    #[test]
    fn interpreter_ignores_a_planted_system_root() {
        check_interpreter_root(Some("C:/fixture-windows"));
    }

    #[test]
    fn interpreter_ignores_a_unc_system_root() {
        check_interpreter_root(Some(r"\\server\windows"));
    }

    #[test]
    fn interpreter_accepts_the_normal_system_root() {
        check_interpreter_root(None);
    }
}
