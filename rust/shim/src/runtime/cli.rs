//! Installer-facing archive-only runtime entry point.
use super::*;
use std::ffi::OsString;

const HELP: &str = "usage: pseudolife-mcp runtime install --archive <archive> --sha256sums <SUMS-file> --version <version> [--json]\n";
#[derive(Debug, PartialEq, Eq)]
struct Options {
    archive: PathBuf,
    sums: PathBuf,
    version: String,
    json: bool,
}
fn parse(args: Vec<OsString>) -> Result<Option<Options>> {
    if args.len() == 1 && ["--help", "-h"].iter().any(|arg| args[0] == *arg) {
        return Ok(None);
    }
    let mut args = args.into_iter();
    if args.next().as_deref() != Some(std::ffi::OsStr::new("install")) {
        return Err("expected runtime install".into());
    }
    let mut archive = None;
    let mut sums = None;
    let mut version = None;
    let mut json = false;
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--help"|"-h")=>return Ok(None),
            Some("--json") if !json=>json=true,
            Some("--archive") if archive.is_none()=>archive=Some(PathBuf::from(args.next().ok_or("--archive requires a file path")?)),
            Some("--sha256sums") if sums.is_none()=>sums=Some(PathBuf::from(args.next().ok_or("--sha256sums requires a file path")?)),
            Some("--version") if version.is_none()=>version=Some(args.next().and_then(|s|s.into_string().ok()).ok_or("--version requires a release version without v")?),
            _=>return Err("unknown or repeated runtime install option; use --archive, --sha256sums, --version and optional --json".into()),
        }
    }
    let options = Options {
        archive: archive.ok_or("--archive is required")?,
        sums: sums.ok_or("--sha256sums is required")?,
        version: version.ok_or("--version is required")?,
        json,
    };
    if options.archive.as_os_str().is_empty()
        || options.sums.as_os_str().is_empty()
        || !valid_version(&options.version)
    {
        return Err(
            "runtime install requires two file paths and a valid release version without v".into(),
        );
    }
    Ok(Some(options))
}
fn bounded_file(path: &Path, limit: u64) -> Result<Vec<u8>> {
    if !plain_file(path) {
        return Err("runtime archive and SHA256SUMS inputs must be readable regular files".into());
    }
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(io_error)?
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    if bytes.len() as u64 > limit {
        return Err("runtime archive or SHA256SUMS exceeds the input size limit".into());
    }
    Ok(bytes)
}
fn install_files(layout: &Layout, options: &Options) -> Result<Value> {
    // Keep downloaded inputs: they are the installer's retry and verification
    // artifacts, not disposable runtime staging directories.
    let archive = bounded_file(&options.archive, 128 * 1024 * 1024)?;
    let sums = bounded_file(&options.sums, 1024 * 1024)?;
    let sums = std::str::from_utf8(&sums).map_err(|_| "SHA256SUMS is not UTF-8")?;
    let _lock = UpdateLock::acquire(layout)?;
    let source = format!("pseudolife-mcp=={}", options.version);
    let runtime = install_archive(layout, &options.version, &source, &archive, sums)?;
    Ok(
        json!({"state":"installed","runtime":runtime.path,"version":runtime.version,"launcher":layout.launcher}),
    )
}

/// Arguments follow the `runtime` mode name. Usage errors exit 2, install or
/// activation errors exit 1, and a verified complete installation exits 0.
pub fn run(args: Vec<OsString>) -> u8 {
    let options = match parse(args) {
        Ok(Some(options)) => options,
        Ok(None) => {
            return emit(HELP.trim_end());
        }
        Err(error) => {
            crate::stderrln!("runtime: {error}\n{}", HELP.trim_end());
            return 2;
        }
    };
    let outcome = default_layout().and_then(|layout| install_files(&layout, &options));
    match outcome {
        Ok(report) => {
            if options.json {
                emit(&report.to_string())
            } else {
                emit(report["launcher"].as_str().unwrap_or(""))
            }
        }
        Err(detail) => {
            if options.json {
                let _ = emit(&json!({"state":"failed","detail":detail}).to_string());
            } else {
                crate::stderrln!(
                    "runtime install failed: {detail}; keep the archive and SHA256SUMS, then retry runtime install"
                );
            }
            1
        }
    }
}

fn emit(text: &str) -> u8 {
    use std::io::Write;
    if writeln!(std::io::stdout().lock(), "{text}").is_ok() {
        0
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(parts: &[&str]) -> Vec<OsString> {
        parts.iter().map(OsString::from).collect()
    }
    #[test]
    fn archive_only_parser_requires_every_input_and_refuses_binary_bypass() {
        for arguments in [
            vec![],
            args(&["install"]),
            args(&["install", "--binary", "artifact"]),
            args(&["install", "--archive", "artifact", "--version", "1.2.3"]),
            args(&[
                "install",
                "--archive",
                "artifact",
                "--sha256sums",
                "SUMS",
                "--version",
                "v1.2.3",
            ]),
            args(&[
                "install",
                "--archive",
                "artifact",
                "--sha256sums",
                "SUMS",
                "--version",
                "1.2.3",
                "--archive",
                "second",
            ]),
        ] {
            assert!(parse(arguments).is_err());
        }
        assert_eq!(
            parse(args(&[
                "install",
                "--archive",
                "with spaces/archive",
                "--sha256sums",
                "SUMS",
                "--version",
                "1.2.3",
                "--json"
            ]))
            .unwrap(),
            Some(Options {
                archive: "with spaces/archive".into(),
                sums: "SUMS".into(),
                version: "1.2.3".into(),
                json: true
            })
        );
    }
    #[test]
    fn archive_install_retains_inputs_and_checksum_failure_keeps_launcher() {
        let directory =
            std::env::temp_dir().join(format!("native-runtime-cli-{}", uuid::Uuid::new_v4()));
        crate::board::state::private_dir(&directory).unwrap();
        let layout = Layout {
            root: directory.join("runtimes"),
            launcher: directory.join("bin").join(BINARY),
            user_bin: None,
            user_path: false,
        };
        let archive = directory.join("archive");
        let sums = directory.join("SHA256SUMS");
        #[cfg(windows)]
        let binary = b"MZ-native-fixture".as_slice();
        #[cfg(target_os = "linux")]
        let binary = b"\x7fELF-native-fixture".as_slice();
        #[cfg(target_os = "macos")]
        let binary = b"\xcf\xfa\xed\xfe-native-fixture".as_slice();
        #[cfg(windows)]
        let data = {
            let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
            writer
                .start_file(BINARY, zip::write::SimpleFileOptions::default())
                .unwrap();
            std::io::Write::write_all(&mut writer, binary).unwrap();
            writer.finish().unwrap().into_inner()
        };
        #[cfg(unix)]
        let data = {
            let encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
            let mut writer = tar::Builder::new(encoder);
            let mut header = tar::Header::new_gnu();
            header.set_mode(0o755);
            header.set_size(binary.len() as u64);
            header.set_cksum();
            writer.append_data(&mut header, BINARY, binary).unwrap();
            writer.into_inner().unwrap().finish().unwrap()
        };
        fs::write(&archive, &data).unwrap();
        let name = install::archive_name("1.2.3").unwrap();
        let correct = format!("{:x}  {name}\n", Sha256::digest(&data));
        fs::write(&sums, &correct).unwrap();
        let options = Options {
            archive: archive.clone(),
            sums: sums.clone(),
            version: "1.2.3".into(),
            json: true,
        };
        let report = install_files(&layout, &options).unwrap();
        assert_eq!(report["state"], "installed");
        assert_eq!(report["version"], "1.2.3");
        assert_eq!(fs::read(&archive).unwrap(), data);
        assert_eq!(fs::read_to_string(&sums).unwrap(), correct);
        let launcher = fs::read(&layout.launcher).unwrap();
        fs::write(&sums, format!("{}  {name}\n", "0".repeat(64))).unwrap();
        assert!(
            install_files(&layout, &options)
                .unwrap_err()
                .contains("checksum mismatch")
        );
        assert_eq!(fs::read(&layout.launcher).unwrap(), launcher);
        assert_eq!(current_runtime(&layout).unwrap().sequence, 1);
        fs::remove_dir_all(directory).unwrap();
    }
}
