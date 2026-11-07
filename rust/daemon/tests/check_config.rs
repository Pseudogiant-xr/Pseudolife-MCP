//! `pseudolife-daemon check-config` as an updater runs it: the process, its
//! exit code and its message, with no database and no bank.

use std::path::{Path, PathBuf};
use std::process::Command;

fn scratch(name: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "pl-check-config-{name}-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&root).unwrap();
    root
}

/// A minimal model root `check-config` admits: the configured graph and the
/// metadata loading reads before it builds a session.
fn model_root(at: &Path) -> PathBuf {
    let root = at.join("model");
    std::fs::create_dir_all(root.join("onnx")).unwrap();
    std::fs::write(root.join("onnx/model.onnx"), b"graph").unwrap();
    std::fs::write(root.join("config.json"), br#"{"hidden_size": 8}"#).unwrap();
    std::fs::write(root.join("tokenizer_config.json"), b"{}").unwrap();
    std::fs::write(root.join("tokenizer.json"), b"{}").unwrap();
    root
}

fn run(dir: &Path, config: &str, onnx_dir: Option<&Path>) -> (i32, String, String) {
    let file = dir.join("config.yaml");
    std::fs::write(&file, config).unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_pseudolife-daemon"));
    command
        .arg("check-config")
        .env_remove("PSEUDOLIFE_MCP_DATABASE_URL")
        .env_remove("PSEUDOLIFE_DAEMON_ONNX_DIR")
        .env_remove("PSEUDOLIFE_EMBEDDING_CPU_DTYPE")
        .env("PSEUDOLIFE_MCP_DATA_DIR", dir)
        .env("PSEUDOLIFE_MCP_CONFIG", &file)
        .env("PSEUDOLIFE_DAEMON_MODELS_DIR", dir.join("models"))
        // Hermetic: no model cache of the machine running the tests.
        .env("HOME", dir)
        .env("USERPROFILE", dir)
        .env("HF_HOME", dir.join("hf"))
        .env_remove("HF_HUB_CACHE")
        .env_remove("HUGGINGFACE_HUB_CACHE")
        .env_remove("XDG_CACHE_HOME");
    if let Some(root) = onnx_dir {
        command.env("PSEUDOLIFE_DAEMON_ONNX_DIR", root);
    }
    let output = command.output().unwrap();
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn a_supported_configuration_passes() {
    let dir = scratch("supported");
    let root = model_root(&dir);
    let (code, out, err) = run(
        &dir,
        "embedding:\n  model_name: custom/model\n",
        Some(&root),
    );
    assert_eq!(code, 0, "{out}{err}");
    assert!(out.contains("supports the configuration"), "{out}");
}

#[test]
fn a_non_default_reranker_is_unsupported_with_its_named_error() {
    let dir = scratch("reranker");
    let root = model_root(&dir);
    let (code, out, err) = run(
        &dir,
        "memory:\n  reranker:\n    model_name: BAAI/bge-reranker-base\n",
        Some(&root),
    );
    assert_eq!(code, 3, "{out}{err}");
    assert!(err.contains("memory.reranker.model_name"), "{err}");
    assert!(err.contains("keep the Python engine"), "{err}");
}

#[test]
fn an_embedding_model_without_an_onnx_graph_is_unsupported() {
    let dir = scratch("embedding");
    let (code, out, err) = run(
        &dir,
        "embedding:\n  model_name: custom/absent-model\n",
        None,
    );
    assert_eq!(code, 3, "{out}{err}");
    assert!(err.contains("embedding.model_name"), "{err}");
}

#[test]
fn bf16_inference_is_unsupported() {
    let dir = scratch("bf16");
    let root = model_root(&dir);
    let (code, out, err) = run(&dir, "embedding:\n  cpu_dtype: bf16\n", Some(&root));
    assert_eq!(code, 3, "{out}{err}");
    assert!(err.contains("bf16"), "{err}");
}

#[test]
fn the_check_never_reaches_the_database() {
    // No database URL: startup would refuse it with 2; the check must not.
    let dir = scratch("nodb");
    let root = model_root(&dir);
    let (code, out, err) = run(
        &dir,
        "embedding:\n  model_name: custom/model\n",
        Some(&root),
    );
    assert_eq!(code, 0, "{out}{err}");
    assert!(!err.contains("PSEUDOLIFE_MCP_DATABASE_URL"), "{err}");
}

#[test]
fn the_pinned_default_model_passes_before_it_is_provisioned() {
    // The updater checks before provisioning; the daemon verifies the model at load.
    let dir = scratch("stock");
    let (code, out, err) = run(&dir, "{}\n", None);
    assert_eq!(code, 0, "{out}{err}");
}

#[test]
fn a_dense_embedding_module_is_unsupported() {
    let dir = scratch("dense");
    let root = model_root(&dir);
    std::fs::write(
        root.join("modules.json"),
        br#"[{"type": "sentence_transformers.models.Transformer", "path": ""},
             {"type": "sentence_transformers.models.Pooling", "path": "1_Pooling"},
             {"type": "sentence_transformers.models.Dense", "path": "2_Dense"}]"#,
    )
    .unwrap();
    std::fs::create_dir_all(root.join("1_Pooling")).unwrap();
    std::fs::write(
        root.join("1_Pooling/config.json"),
        br#"{"word_embedding_dimension": 8, "pooling_mode_mean_tokens": true}"#,
    )
    .unwrap();
    let (code, out, err) = run(
        &dir,
        "embedding:\n  model_name: custom/model\n",
        Some(&root),
    );
    assert_eq!(code, 3, "{out}{err}");
    assert!(err.contains("Dense"), "{err}");
}

#[test]
fn an_unreadable_sweep_interval_is_unsupported() {
    let dir = scratch("sweep");
    let root = model_root(&dir);
    let (code, out, err) = run(
        &dir,
        "memory:\n  dream:\n    enabled: true\n    sweep_interval_seconds: soon\n",
        Some(&root),
    );
    assert_eq!(code, 3, "{out}{err}");
    assert!(err.contains("sweep_interval_seconds"), "{err}");
}
