//! Install-time model downloads. Serving only reads and verifies these pins.
//!
//! The shared native provisioner stages the complete bundle under its install
//! lock, verifies downloads, and publishes the tree with one directory rename.

use crate::native::native_provision::{self as native, Fetch, Manifest, Pinned};
use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub const EMBEDDING_MODEL: &str = "Qwen/Qwen3-Embedding-0.6B";
pub const CACHE_ENV: &str = "PSEUDOLIFE_DAEMON_MODELS_DIR";
const COMMAND: &str = "pseudolife-daemon provision models";

struct Artifact {
    path: &'static str,
    url: &'static str,
    sha256: &'static str,
    bytes: u64,
}

impl Pinned for Artifact {
    fn url(&self) -> &str {
        self.url
    }
    fn sha256(&self) -> &str {
        self.sha256
    }
}

macro_rules! artifact {
    ($path:literal, $repo:literal, $revision:literal, $source:literal, $sha:literal, $bytes:literal) => {
        Artifact {
            path: $path,
            url: concat!(
                "https://huggingface.co/",
                $repo,
                "/resolve/",
                $revision,
                "/",
                $source
            ),
            sha256: $sha,
            bytes: $bytes,
        }
    };
}

// One fp32 export per model. Metadata is pinned as well as the graph and
// external tensors because tokenization and pooling affect the result.
const ARTIFACTS: &[Artifact] = &[
    artifact!(
        "embedding/onnx/model.onnx",
        "onnx-community/ONNX_Qwen3-Embedding-0.6B",
        "462e5a71e724575c710975d9b79309b690fd22ce",
        "model.onnx",
        "cec22565ec783289a5e51bd94950f70b8cb7ca6c0b7ced255b5e8dbf3c60536b",
        1241005
    ),
    artifact!(
        "embedding/onnx/model.onnx_data",
        "onnx-community/ONNX_Qwen3-Embedding-0.6B",
        "462e5a71e724575c710975d9b79309b690fd22ce",
        "model.onnx_data",
        "a585477de21c0a89e021dd64f4d3be34483eb4aaed7ae93fc047e3edf74545da",
        2383106304
    ),
    artifact!(
        "embedding/tokenizer.json",
        "onnx-community/ONNX_Qwen3-Embedding-0.6B",
        "462e5a71e724575c710975d9b79309b690fd22ce",
        "tokenizer.json",
        "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a",
        11423705
    ),
    artifact!(
        "embedding/modules.json",
        "Qwen/Qwen3-Embedding-0.6B",
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "modules.json",
        "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
        349
    ),
    artifact!(
        "embedding/1_Pooling/config.json",
        "Qwen/Qwen3-Embedding-0.6B",
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "1_Pooling/config.json",
        "37bf193fa101f19101bfad9c31d3eb0f786e247b7b1e5cb7f007d730eed1ddbd",
        313
    ),
    artifact!(
        "embedding/config.json",
        "Qwen/Qwen3-Embedding-0.6B",
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "config.json",
        "b5bf1f51fc45be473a54718cef92448d90a1be001bf9b9a44b8c7f10a19feaa9",
        727
    ),
    artifact!(
        "embedding/tokenizer_config.json",
        "Qwen/Qwen3-Embedding-0.6B",
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "tokenizer_config.json",
        "253153d0738ceb4c668d2eff957714dd2bea0b56de772a9fdccd96cbf517e6a0",
        9706
    ),
    artifact!(
        "embedding/config_sentence_transformers.json",
        "Qwen/Qwen3-Embedding-0.6B",
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "config_sentence_transformers.json",
        "10667c72ddb772627bf1780cb7f86af8e2ae0032b8c243c731172064105c6961",
        215
    ),
    artifact!(
        "reranker/model.onnx",
        "cross-encoder/ms-marco-MiniLM-L6-v2",
        "c5ee24cb16019beea0893ab7796b1df96625c6b8",
        "onnx/model.onnx",
        "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a",
        91011230
    ),
    artifact!(
        "reranker/tokenizer.json",
        "cross-encoder/ms-marco-MiniLM-L6-v2",
        "c5ee24cb16019beea0893ab7796b1df96625c6b8",
        "tokenizer.json",
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        711396
    ),
    artifact!(
        "reranker/config.json",
        "cross-encoder/ms-marco-MiniLM-L6-v2",
        "c5ee24cb16019beea0893ab7796b1df96625c6b8",
        "config.json",
        "380e02c93f431831be65d99a4e7e5f67c133985bf2e77d9d4eba46847190bacc",
        794
    ),
    artifact!(
        "reranker/tokenizer_config.json",
        "cross-encoder/ms-marco-MiniLM-L6-v2",
        "c5ee24cb16019beea0893ab7796b1df96625c6b8",
        "tokenizer_config.json",
        "a5c2e5a7b1a29a0702cd28c08a399b5ecc110c263009d17f7e3b415f25905fd8",
        1330
    ),
];

fn manifest(artifacts: &[Artifact]) -> Manifest<'_, Artifact> {
    Manifest {
        label: "embedding and reranker models",
        version: "qwen3-msmarco-fp32-v1",
        artifacts,
        record: serde_json::json!({"layout": artifacts.iter().map(|a| a.path).collect::<Vec<_>>()})
            .as_object()
            .unwrap()
            .clone(),
    }
}

fn cache_root_from(env: impl Fn(&str) -> Option<OsString>) -> Result<PathBuf> {
    let set = |name: &str| env(name).filter(|v| !v.is_empty()).map(PathBuf::from);
    if let Some(root) = set(CACHE_ENV) {
        return Ok(root);
    }
    if let Some(data) = set("PSEUDOLIFE_MCP_DATA_DIR") {
        return Ok(data.join("models"));
    }
    let home = set(if cfg!(windows) { "USERPROFILE" } else { "HOME" });
    native::default_data_dir(native::Platform::current(), &env, home.as_deref())
        .map(|data| data.join("models"))
        .with_context(|| {
            format!("no model cache directory is known; set {CACHE_ENV} and run `{COMMAND}`")
        })
}

pub fn cache_root() -> Result<PathBuf> {
    cache_root_from(|key| std::env::var_os(key))
}

pub fn embedding_root() -> Result<PathBuf> {
    Ok(manifest(ARTIFACTS)
        .install_dir(&cache_root()?)
        .join("embedding"))
}

pub fn reranker_root() -> Result<PathBuf> {
    if let Some(root) = std::env::var_os("PSEUDOLIFE_DAEMON_RERANK_DIR").filter(|v| !v.is_empty()) {
        return Ok(root.into());
    }
    Ok(manifest(ARTIFACTS)
        .install_dir(&cache_root()?)
        .join("reranker"))
}

fn repair_directory(root: &Path) -> &Path {
    let bundle = manifest(ARTIFACTS).install_dir(Path::new(""));
    if matches!(
        root.file_name().and_then(|n| n.to_str()),
        Some("embedding" | "reranker")
    ) && let Some(parent) = root.parent()
        && parent.file_name() == bundle.file_name()
    {
        return parent;
    }
    root
}

pub(super) fn missing_repair(root: &Path) -> String {
    let repair = repair_directory(root);
    if repair.symlink_metadata().is_ok() {
        format!(
            "move {} aside and run `{COMMAND}` (remove a model-root override pointing to the moved directory)",
            repair.display()
        )
    } else {
        format!("run `{COMMAND}` with the same model cache settings")
    }
}

fn verify_file(root: &Path, relative: &str, expected: &str, hub: bool) -> Result<()> {
    let path = root.join(relative);
    let parts = super::relative_parts(relative)?;
    if !super::existing_file(root, &parts, hub) {
        bail!(
            "model artifact is missing or unsafe: {}; {}",
            path.display(),
            missing_repair(root)
        );
    }
    let mut input = fs::File::open(&path)
        .with_context(|| format!("cannot read model artifact {}", path.display()))?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let actual = hex::encode(digest.finalize());
    if actual != expected {
        bail!(
            "model checksum mismatch for {}: expected {expected}, got {actual}; move {} aside and run `{COMMAND}` (remove a model-root override pointing to the moved directory)",
            path.display(),
            repair_directory(root).display()
        );
    }
    Ok(())
}

fn verify_model(root: &Path, prefix: &str, hub: bool) -> Result<()> {
    for artifact in ARTIFACTS {
        if let Some(relative) = artifact.path.strip_prefix(prefix) {
            verify_file(root, relative, artifact.sha256, hub)?;
        }
    }
    Ok(())
}

pub fn verify_embedding(root: &Path, file: &str, hub: bool) -> Result<()> {
    if file != "onnx/model.onnx" {
        bail!(
            "the pinned Qwen export requires embedding.onnx_file_name=onnx/model.onnx; restore that setting and run `{COMMAND}`"
        );
    }
    verify_model(root, "embedding/", hub)?;
    // This checkpoint has no sentence_bert_config.json. The loader would
    // otherwise consume an unpinned sequence cap or lowercasing policy.
    if root
        .join("sentence_bert_config.json")
        .symlink_metadata()
        .is_ok()
    {
        bail!(
            "unpinned Qwen sentence_bert_config.json in {}; move it aside and run `{COMMAND}`",
            root.display()
        );
    }
    Ok(())
}

pub fn verify_reranker(root: &Path) -> Result<()> {
    verify_model(root, "reranker/", false)
}

/// Limit bytes before they reach the shared provisioner's disk sink. The
/// checksum still decides integrity; this cap bounds a bad server's disk use.
struct SizedFetch<'a> {
    artifacts: &'a [Artifact],
    inner: &'a dyn Fetch,
}

struct LimitedWriter<'a> {
    sink: &'a mut dyn Write,
    remaining: u64,
}

impl Write for LimitedWriter<'_> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() as u64 > self.remaining {
            return Err(std::io::Error::other(
                "download exceeds the pinned artifact byte size",
            ));
        }
        let written = self.sink.write(bytes)?;
        self.remaining -= written as u64;
        Ok(written)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.sink.flush()
    }
}

impl Fetch for SizedFetch<'_> {
    fn fetch(&self, url: &str, sink: &mut dyn Write) -> std::result::Result<(), String> {
        let artifact = self
            .artifacts
            .iter()
            .find(|a| a.url == url)
            .ok_or("download is not in the pinned manifest")?;
        let mut limited = LimitedWriter {
            sink,
            remaining: artifact.bytes,
        };
        self.inner.fetch(url, &mut limited)?;
        if limited.remaining != 0 {
            return Err(format!(
                "download is shorter than the pinned {} bytes",
                artifact.bytes
            ));
        }
        Ok(())
    }
}

fn install(
    root: &Path,
    artifacts: &[Artifact],
    fetch: &dyn Fetch,
    log: &mut dyn FnMut(&str),
) -> Result<(PathBuf, bool)> {
    let manifest = manifest(artifacts);
    let fetch = SizedFetch {
        artifacts,
        inner: fetch,
    };
    native::provision(root, &manifest, &fetch, &mut |line| {
        // A successful rerun has exactly one user-facing line below.
        if !line.contains(" already installed at ") { log(line); }
    }, &mut |artifact, download, tree| {
        let target = tree.join(artifact.path);
        native::create_dir(target.parent().unwrap()).map_err(|e| e.to_string())?;
        let mut input = fs::File::open(download).map_err(|e| e.to_string())?;
        native::write_new(&target, &mut input, 0o644).map_err(|e| e.to_string())
    }, &|tree| {
        for artifact in artifacts {
            verify_file(tree, artifact.path, artifact.sha256, false).map_err(|e| e.to_string())?;
        }
        Ok(tree.to_path_buf())
    }).map_err(|error| {
        let fix = match &error {
            native::Error::Download { .. } => format!("check network access and retry `{COMMAND}`"),
            native::Error::Installed { .. } => format!("move {} aside and run `{COMMAND}`", manifest.install_dir(root).display()),
            native::Error::Io { .. } => format!("correct the filesystem error in {} and retry `{COMMAND}`", root.display()),
            _ => format!("retry `{COMMAND}`; if it repeats, report the pinned model installation error without bypassing the checksum"),
        };
        anyhow::anyhow!("{error}; {fix}")
    })
}

fn needs_install(root: &Path, artifacts: &[Artifact]) -> Result<bool> {
    let bundle = manifest(artifacts).install_dir(root);
    match bundle.symlink_metadata() {
        Ok(_) => Ok(false),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(true),
        Err(error) => {
            Err(error).with_context(|| format!("cannot inspect model bundle {}", bundle.display()))
        }
    }
}

pub fn command(if_missing: bool) -> i32 {
    let result = cache_root().and_then(|root| {
        // The entrypoint only installs an absent bundle. It never asserts
        // readiness: the daemon hashes the embedding at load and the optional
        // reranker hashes its own inputs at first use. Explicit provisioning
        // without this flag retains full installed-bundle verification.
        if if_missing && !needs_install(&root, ARTIFACTS)? {
            println!("model bundle present; the daemon will verify each model before loading");
            return Ok((root, true));
        }
        if if_missing {
            println!("model provisioning phase: installing pinned artifacts; daemon readiness follows this phase");
        }
        install(&root, ARTIFACTS, &native::HttpFetch, &mut |line| {
            println!("{line}")
        })
    });
    match result {
        Ok((root, false)) => {
            println!("models already provisioned at {}", root.display());
            0
        }
        Ok((_, true)) => 0,
        Err(error) => {
            eprintln!("{COMMAND}: {error:#}; no new model bundle was installed.");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    const GOOD: &str = "770e607624d689265ca6c44884d0807d9b054d23c473c106c72be9de08b7376c";
    const FIXTURE: &[Artifact] = &[Artifact {
        path: "embedding/onnx/model.onnx",
        url: "https://example.com/model.onnx",
        sha256: GOOD,
        bytes: 4,
    }];
    struct Bytes(&'static [u8]);
    impl Fetch for Bytes {
        fn fetch(&self, _: &str, sink: &mut dyn Write) -> std::result::Result<(), String> {
            sink.write_all(self.0).map_err(|e| e.to_string())
        }
    }
    struct Offline;
    impl Fetch for Offline {
        fn fetch(&self, _: &str, _: &mut dyn Write) -> std::result::Result<(), String> {
            panic!("offline rerun tried downloading")
        }
    }
    fn temp() -> PathBuf {
        std::env::temp_dir().join(format!("pl-models-{}", uuid::Uuid::new_v4()))
    }

    #[test]
    fn complete_bundle_is_idempotent_offline_and_tampering_is_refused() {
        let root = temp();
        assert!(needs_install(&root, FIXTURE).unwrap());
        let (installed, fresh) = install(&root, FIXTURE, &Bytes(b"good"), &mut |_| {}).unwrap();
        assert!(fresh);
        let mut messages = vec![];
        let (again, fresh) = install(&root, FIXTURE, &Offline, &mut |line| {
            messages.push(line.to_string())
        })
        .unwrap();
        assert!(!fresh);
        assert_eq!(again, installed);
        assert!(messages.is_empty());
        fs::write(installed.join(FIXTURE[0].path), b"bad").unwrap();
        // The entrypoint only checks presence; it does not attest integrity.
        assert!(!needs_install(&root, FIXTURE).unwrap());
        assert!(verify_file(&installed, FIXTURE[0].path, GOOD, false).is_err());
        let error = install(&root, FIXTURE, &Offline, &mut |_| {})
            .unwrap_err()
            .to_string();
        assert!(error.contains("checksum mismatch"), "{error}");
        assert!(error.contains(COMMAND), "{error}");
        // Rehearse the repair named by the refusal: the complete bundle,
        // rather than only its embedding subdirectory, is moved aside.
        fs::rename(&installed, root.join("changed-bundle")).unwrap();
        assert!(
            install(&root, FIXTURE, &Bytes(b"good"), &mut |_| {})
                .unwrap()
                .1
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn bad_download_never_publishes_and_a_retry_recovers() {
        let root = temp();
        for body in [&b"bad"[..], &b"too large"[..]] {
            assert!(install(&root, FIXTURE, &Bytes(body), &mut |_| {}).is_err());
        }
        assert!(!manifest(FIXTURE).install_dir(&root).exists());
        assert!(!fs::read_dir(&root).unwrap().any(|e| {
            e.unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".staging-")
        }));
        assert!(
            install(&root, FIXTURE, &Bytes(b"good"), &mut |_| {})
                .unwrap()
                .1
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn download_size_cap_rejects_excess_and_short_bodies() {
        let mut sink = Vec::new();
        let fetch = SizedFetch {
            artifacts: FIXTURE,
            inner: &Bytes(b"too large"),
        };
        assert!(
            fetch
                .fetch(FIXTURE[0].url, &mut sink)
                .unwrap_err()
                .contains("exceeds")
        );
        assert!(sink.is_empty());
        let fetch = SizedFetch {
            artifacts: FIXTURE,
            inner: &Bytes(b"bad"),
        };
        assert!(
            fetch
                .fetch(FIXTURE[0].url, &mut sink)
                .unwrap_err()
                .contains("shorter")
        );
        assert_eq!(sink, b"bad");
    }

    #[test]
    fn missing_file_names_a_repair_that_reinstalls_the_bundle() {
        let root = temp();
        let (installed, _) = install(&root, FIXTURE, &Bytes(b"good"), &mut |_| {}).unwrap();
        fs::remove_file(installed.join(FIXTURE[0].path)).unwrap();
        let error = verify_file(&installed, FIXTURE[0].path, GOOD, false)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains(&format!("move {} aside", installed.display())),
            "{error}"
        );
        assert!(error.contains(COMMAND), "{error}");
        fs::rename(&installed, root.join("incomplete-bundle")).unwrap();
        assert!(
            install(&root, FIXTURE, &Bytes(b"good"), &mut |_| {})
                .unwrap()
                .1
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn every_consumed_artifact_is_verified_before_loading() {
        let root = temp();
        fs::create_dir_all(&root).unwrap();
        for prefix in ["embedding/", "reranker/"] {
            let error = verify_model(&root, prefix, false).unwrap_err().to_string();
            assert!(error.contains("missing or unsafe"), "{error}");
            assert!(error.contains(COMMAND), "{error}");
        }
        fs::write(root.join("model.onnx"), b"good").unwrap();
        verify_file(&root, "model.onnx", GOOD, false).unwrap();
        fs::write(root.join("model.onnx"), b"bad").unwrap();
        assert!(
            verify_file(&root, "model.onnx", GOOD, false)
                .unwrap_err()
                .to_string()
                .contains("checksum mismatch")
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn persistent_cache_precedes_user_home() {
        assert_eq!(
            cache_root_from(
                |key| (key == "PSEUDOLIFE_MCP_DATA_DIR").then(|| OsString::from("data"))
            )
            .unwrap(),
            Path::new("data/models")
        );
        assert_eq!(
            cache_root_from(|key| match key {
                CACHE_ENV => Some("models".into()),
                "PSEUDOLIFE_MCP_DATA_DIR" => Some("data".into()),
                _ => None,
            })
            .unwrap(),
            Path::new("models")
        );
    }

    #[test]
    fn repair_names_the_whole_bundle_but_never_a_custom_models_parent() {
        let root = temp();
        let bundle = manifest(ARTIFACTS).install_dir(&root);
        assert_eq!(repair_directory(&bundle.join("embedding")), bundle);
        assert_eq!(repair_directory(&bundle.join("reranker")), bundle);
        assert!(!missing_repair(&bundle.join("embedding")).contains("move "));
        fs::create_dir_all(&bundle).unwrap();
        assert!(
            missing_repair(&bundle.join("embedding"))
                .contains(&format!("move {} aside", bundle.display()))
        );
        assert_eq!(
            repair_directory(Path::new("custom/embedding")),
            Path::new("custom/embedding")
        );
        fs::remove_dir_all(root).unwrap();
    }
}
