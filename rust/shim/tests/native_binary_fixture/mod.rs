use std::path::{Path, PathBuf};

/// Exercise release installation with a private executable copy, without debug
/// symbols. The merged Linux debug CLI reached 271,712,832 bytes, exceeding the
/// installer's 256 MiB release limit before lifecycle assertions could run.
/// Keep that production limit and never modify Cargo's shared executable.
pub fn installable(source: &Path, directory: &Path) -> PathBuf {
    #[cfg(unix)]
    {
        let binary = directory.join("installable-cli");
        std::fs::copy(source, &binary).expect("copy native runtime fixture");
        let output = std::process::Command::new("strip")
            .arg("-S")
            .arg(&binary)
            .output()
            .expect("native runtime fixture needs strip (binutils or Xcode command-line tools)");
        assert!(
            output.status.success(),
            "strip native runtime fixture: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        binary
    }
    #[cfg(not(unix))]
    {
        let _ = directory;
        source.to_path_buf()
    }
}
