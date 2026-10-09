//! Query embedding: Qwen3-Embedding-0.6B through ONNX Runtime.
//! Last-token pooling and L2 normalization, as the prerequisite receipt ran it.

use anyhow::{Result, anyhow};
use ort::session::Session;
use ort::value::Tensor;
use std::path::Path;
use std::sync::Mutex;
use tokenizers::{Tokenizer, TruncationParams};

/// `EmbeddingConfig.query_prefix` (utils/config.py:44): no space after "Query:".
pub const QUERY_PREFIX: &str =
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:";
/// `EmbeddingConfig.max_seq_length` (utils/config.py:54).
pub const MAX_TOKENS: usize = 512;

pub struct Embedder {
    tokenizer: Tokenizer,
    session: Mutex<Session>,
}

impl Embedder {
    /// `model_dir` holds `model.onnx` (+ `model.onnx_data`) and `tokenizer.json`.
    pub fn load(model_dir: &Path, threads: usize) -> Result<Self> {
        let mut tokenizer =
            Tokenizer::from_file(model_dir.join("tokenizer.json")).map_err(|e| anyhow!("{e}"))?;
        tokenizer
            .with_truncation(Some(TruncationParams {
                max_length: MAX_TOKENS,
                ..Default::default()
            }))
            .map_err(|e| anyhow!("{e}"))?;
        tokenizer.with_padding(None);
        let session = Session::builder()?
            .with_intra_threads(threads)?
            .commit_from_file(model_dir.join("model.onnx"))?;
        Ok(Embedder {
            tokenizer,
            session: Mutex::new(session),
        })
    }

    /// Embed text as given (callers add the query prefix).
    pub fn embed(&self, text: &str) -> Result<Vec<f32>> {
        let enc = self
            .tokenizer
            .encode(text, true)
            .map_err(|e| anyhow!("{e}"))?;
        let ids: Vec<i64> = enc.get_ids().iter().map(|&x| x as i64).collect();
        let t = ids.len();
        if t == 0 {
            return Err(anyhow!("empty tokenization"));
        }
        let mask: Vec<i64> = enc.get_attention_mask().iter().map(|&x| x as i64).collect();
        let pos: Vec<i64> = (0..t as i64).collect();
        let mut session = self.session.lock().unwrap();
        let outputs = session.run(ort::inputs![
            "input_ids" => Tensor::from_array(([1usize, t], ids))?,
            "attention_mask" => Tensor::from_array(([1usize, t], mask))?,
            "position_ids" => Tensor::from_array(([1usize, t], pos))?,
        ])?;
        let (shape, data) = outputs["last_hidden_state"].try_extract_tensor::<f32>()?;
        let dim = *shape.last().ok_or_else(|| anyhow!("bad output shape"))? as usize;
        let mut v = data[(t - 1) * dim..t * dim].to_vec();
        crate::bank::normalize_query(&mut v);
        Ok(v)
    }

    pub fn embed_query(&self, q: &str) -> Result<Vec<f32>> {
        self.embed(&format!("{QUERY_PREFIX}{q}"))
    }
}
