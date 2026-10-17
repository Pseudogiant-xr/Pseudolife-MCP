//! Cross-encoder reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` through ONNX
//! Runtime, reproducing `pseudolife_memory/memory/reranker.py`
//! (`CrossEncoderReranker.rerank` / `fuse` / `_sigmoid`).
//!
//! Python scores pairs with sentence-transformers' `CrossEncoder.predict`:
//! the BERT fast tokenizer is called on `[query, candidate]` pairs with
//! `padding=True, truncation="longest_first"` and no explicit `max_length`, so
//! it truncates at the tokenizer's `model_max_length` (512, capped by
//! `max_position_embeddings`). Pairs are sorted by descending character length,
//! padded to the longest pair of each batch of 32, and the model's single
//! logit is returned untouched (the model config names `Identity` as the
//! activation). `reranker.py` then applies a numerically stable sigmoid.
//!
//! This port tokenizes each pair the same way, groups them into the same
//! length-sorted batches of 32, right-pads each batch with `[PAD]` (id 0,
//! type id 0, attention 0) and runs the batch in one session call. Padding
//! under an attention mask changes a logit only by float noise.

use anyhow::{Result, anyhow};
use ndarray::Array2;
use ort::session::Session;
use ort::value::Tensor;
use std::path::Path;
use std::sync::Mutex;
use tokenizers::{
    EncodeInput, Tokenizer, TruncationDirection, TruncationParams, TruncationStrategy,
};

/// Tokenizer `model_max_length` as `CrossEncoder` resolves it: 512 from
/// `tokenizer_config.json`, capped by `max_position_embeddings` (512).
pub const MAX_TOKENS: usize = 512;
/// `CrossEncoder.predict(batch_size=32)` default.
pub const BATCH_SIZE: usize = 32;

pub struct Reranker {
    tokenizer: Tokenizer,
    session: Mutex<Session>,
}

impl Reranker {
    /// dir holds model.onnx and tokenizer.json (an optimum text-classification export).
    pub fn load(dir: &Path, threads: usize) -> Result<Reranker> {
        let mut tokenizer =
            Tokenizer::from_file(dir.join("tokenizer.json")).map_err(|e| anyhow!("{e}"))?;
        // The exported tokenizer.json carries no truncation; Python passes
        // truncation="longest_first" at call time with max_length 512.
        tokenizer
            .with_truncation(Some(TruncationParams {
                max_length: MAX_TOKENS,
                strategy: TruncationStrategy::LongestFirst,
                stride: 0,
                direction: TruncationDirection::Right,
            }))
            .map_err(|e| anyhow!("{e}"))?;
        // Padding is done per batch below, so the tokenizer pads nothing.
        tokenizer.with_padding(None);
        let session = Session::builder()?
            .with_intra_threads(threads)?
            .commit_from_file(dir.join("model.onnx"))?;
        Ok(Reranker {
            tokenizer,
            session: Mutex::new(session),
        })
    }

    /// reranker.py rerank(): one sigmoid(logit) per candidate, same order. Empty Vec when
    /// candidates is empty or query.trim() is empty.
    pub fn rerank(&self, query: &str, candidates: &[&str]) -> Result<Vec<f64>> {
        if candidates.is_empty() || py_strip_is_empty(query) {
            return Ok(Vec::new());
        }
        Ok(self
            .logits(query, candidates)?
            .into_iter()
            .map(|x| sigmoid(f64::from(x)))
            .collect())
    }

    /// Raw logits, for the parity check. Like `CrossEncoder.predict`, this
    /// applies no blank-query gate; empty candidates give an empty Vec.
    pub fn logits(&self, query: &str, candidates: &[&str]) -> Result<Vec<f32>> {
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        // predict(): np.argsort([-len(q) - len(c) ...]); Python string length
        // counts code points. A stable sort only reorders ties, which share a
        // batch either way except at a 32-pair boundary (float noise at most).
        let qlen = query.chars().count();
        let mut order: Vec<usize> = (0..candidates.len()).collect();
        order.sort_by_key(|&i| std::cmp::Reverse(qlen + candidates[i].chars().count()));

        let mut out = vec![0f32; candidates.len()];
        for chunk in order.chunks(BATCH_SIZE) {
            let mut encs = Vec::with_capacity(chunk.len());
            for &i in chunk {
                let input: EncodeInput = (query, candidates[i]).into();
                encs.push(
                    self.tokenizer
                        .encode(input, true)
                        .map_err(|e| anyhow!("{e}"))?,
                );
            }
            let width = encs.iter().map(|e| e.len()).max().unwrap_or(0);
            if width == 0 {
                return Err(anyhow!("empty tokenization"));
            }
            let rows = encs.len();
            let mut ids = Array2::<i64>::zeros((rows, width));
            let mut mask = Array2::<i64>::zeros((rows, width));
            let mut types = Array2::<i64>::zeros((rows, width));
            for (r, enc) in encs.iter().enumerate() {
                let parts = enc
                    .get_ids()
                    .iter()
                    .zip(enc.get_attention_mask())
                    .zip(enc.get_type_ids());
                for (c, ((&id, &m), &t)) in parts.enumerate() {
                    ids[[r, c]] = i64::from(id);
                    mask[[r, c]] = i64::from(m);
                    types[[r, c]] = i64::from(t);
                }
            }
            let mut session = self
                .session
                .lock()
                .map_err(|_| anyhow!("reranker session lock poisoned"))?;
            let outputs = session.run(ort::inputs![
                "input_ids" => Tensor::from_array(ids)?,
                "attention_mask" => Tensor::from_array(mask)?,
                "token_type_ids" => Tensor::from_array(types)?,
            ])?;
            let (shape, data) = outputs["logits"].try_extract_tensor::<f32>()?;
            if shape.len() != 2 || shape[0] as usize != rows || shape[1] != 1 {
                return Err(anyhow!("unexpected logits shape {shape:?}"));
            }
            for (k, &i) in chunk.iter().enumerate() {
                out[i] = data[k];
            }
        }
        Ok(out)
    }
}

/// `not query.strip()`: Python's `str.isspace` set is Unicode `White_Space`
/// plus the information separators U+001C..U+001F.
fn py_strip_is_empty(s: &str) -> bool {
    s.chars()
        .all(|c| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// reranker.py _sigmoid (stable form, f64).
pub fn sigmoid(x: f64) -> f64 {
    if x >= 0.0 {
        let z = (-x).exp();
        1.0 / (1.0 + z)
    } else {
        let z = x.exp();
        z / (1.0 + z)
    }
}

/// reranker.py fuse(): originals pass through when ce is empty; w*ce + (1-w)*orig otherwise.
///
/// Python raises `ValueError` on a length mismatch; with this signature the
/// mismatch is an invariant violation and panics with the same message. CMS
/// always passes the scores of the very candidates it fuses, so it cannot
/// occur there.
pub fn fuse(weight: f64, originals: &[f64], ce: &[f64]) -> Vec<f64> {
    if ce.is_empty() {
        return originals.to_vec();
    }
    assert_eq!(
        ce.len(),
        originals.len(),
        "fuse: length mismatch - originals={}, ce_scores={}",
        originals.len(),
        ce.len()
    );
    originals
        .iter()
        .zip(ce)
        .map(|(&orig, &c)| weight * c + (1.0 - weight) * orig)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sigmoid_matches_python_values() {
        assert_eq!(sigmoid(0.0), 0.5);
        // math values from Python's _sigmoid.
        assert!((sigmoid(2.0) - 0.8807970779778823).abs() < 1e-15);
        assert!((sigmoid(-2.0) - 0.11920292202211755).abs() < 1e-15);
        assert!((sigmoid(1.0) + sigmoid(-1.0) - 1.0).abs() < 1e-15);
    }

    #[test]
    fn sigmoid_is_stable_at_extremes() {
        assert_eq!(sigmoid(1000.0), 1.0);
        assert_eq!(sigmoid(-1000.0), 0.0);
        assert_eq!(sigmoid(f64::MAX), 1.0);
        assert_eq!(sigmoid(f64::MIN), 0.0);
        assert_eq!(sigmoid(f64::INFINITY), 1.0);
        assert_eq!(sigmoid(f64::NEG_INFINITY), 0.0);
        // -745 is near the subnormal floor: the stable branch keeps it nonzero.
        assert!(sigmoid(-745.0) > 0.0);
        assert!(sigmoid(-40.0) > 0.0 && sigmoid(-40.0) < 1e-17);
        assert!(sigmoid(f64::NAN).is_nan());
    }

    #[test]
    fn fuse_passes_originals_through_when_ce_empty() {
        assert_eq!(fuse(0.7, &[0.3, 0.9], &[]), vec![0.3, 0.9]);
        assert_eq!(fuse(0.7, &[], &[]), Vec::<f64>::new());
    }

    #[test]
    fn fuse_is_weighted_sum() {
        let got = fuse(0.7, &[0.2, 1.0], &[1.0, 0.0]);
        assert!((got[0] - (0.7 + 0.3 * 0.2)).abs() < 1e-15);
        assert!((got[1] - 0.3).abs() < 1e-15);
        assert_eq!(fuse(1.0, &[0.2], &[0.9]), vec![0.9]);
        assert_eq!(fuse(0.0, &[0.2], &[0.9]), vec![0.2]);
    }

    #[test]
    #[should_panic(expected = "length mismatch")]
    fn fuse_rejects_length_mismatch() {
        fuse(0.5, &[0.1, 0.2], &[0.3]);
    }

    #[test]
    fn blank_query_matches_python_strip() {
        assert!(py_strip_is_empty(""));
        assert!(py_strip_is_empty(" \t\n\r\u{b}\u{c}"));
        assert!(py_strip_is_empty("\u{a0}\u{3000}\u{2028}\u{85}"));
        assert!(py_strip_is_empty("\u{1c}\u{1d}\u{1e}\u{1f}"));
        assert!(!py_strip_is_empty(" a "));
        assert!(!py_strip_is_empty("\u{200b}"));
    }

    /// Parity dump for `harness/w2d_rerank_check.py`: reads
    /// `[{"query": .., "candidates": [..]}]` from `W2D_RERANK_CASES`, writes
    /// `[{"logits": [..], "rerank": [..]}]` to `W2D_RERANK_OUT`. The model
    /// directory is `W2D_RERANK_MODEL_DIR`; ONNX Runtime comes from `ORT_DYLIB_PATH`.
    #[test]
    #[ignore]
    fn rerank_dump() {
        let env = |k: &str| std::env::var(k).unwrap_or_else(|_| panic!("{k} not set"));
        let dir = std::path::PathBuf::from(env("W2D_RERANK_MODEL_DIR"));
        let cases: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(env("W2D_RERANK_CASES")).unwrap())
                .unwrap();
        let rr = Reranker::load(&dir, 2).unwrap();
        let mut out = Vec::new();
        for case in cases.as_array().unwrap() {
            let query = case["query"].as_str().unwrap();
            let cands: Vec<&str> = case["candidates"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect();
            let logits = rr.logits(query, &cands).unwrap();
            let rerank = rr.rerank(query, &cands).unwrap();
            out.push(serde_json::json!({ "logits": logits, "rerank": rerank }));
        }
        std::fs::write(
            env("W2D_RERANK_OUT"),
            serde_json::to_string(&serde_json::Value::Array(out)).unwrap(),
        )
        .unwrap();
    }
}
