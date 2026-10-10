//! Shared load-only CPU ONNX session setup for embedding and reranking.

use anyhow::{Result, bail};
use ort::execution_providers::CPUExecutionProvider;
use ort::session::{Session, builder::GraphOptimizationLevel};
use std::path::Path;

pub fn load_cpu_session(graph: &Path, threads: usize) -> Result<Session> {
    if threads == 0 || !graph.is_file() {
        bail!("ONNX requires an existing graph and positive CPU thread count");
    }
    // `load-dynamic` links an installed local runtime; `download-binaries`
    // is disabled in Cargo.toml. No accelerator is registered.
    Ok(Session::builder()?
        .with_execution_providers([CPUExecutionProvider::default().build()])?
        .with_intra_threads(threads)?
        .with_inter_threads(1)?
        .with_optimization_level(GraphOptimizationLevel::Level3)?
        .commit_from_file(graph)?)
}
