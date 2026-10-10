//! Load-only ONNX document/query encoding with the Python pipeline's LRU.

use crate::{
    config::EmbeddingConfig,
    embedding_math::{self, Pooling},
    onnx_artifacts as artifacts,
};
use anyhow::{Result, anyhow, bail};
use ort::{session::Session, value::Tensor};
use serde_json::Value;
use std::{
    collections::VecDeque,
    path::{Path, PathBuf},
    sync::Mutex,
};
use tokenizers::{
    PaddingDirection, PaddingParams, Tokenizer, TruncationDirection, TruncationParams,
};

struct State {
    session: Session,
    cache: VecDeque<((String, bool), Vec<f32>)>,
    forwards: usize,
}

pub struct Embedder {
    config: EmbeddingConfig,
    tokenizer: Tokenizer,
    state: Mutex<State>,
    pooling: Pooling,
    always_normalize: bool,
    lower_case: bool,
    dim: usize,
    pub model_root: PathBuf,
    pub graph: PathBuf,
    tokenizer_file: PathBuf,
}

fn metadata(root: &Path, relative: &str, hub: bool) -> Result<Value> {
    let parts = artifacts::relative_parts(relative)?;
    if !artifacts::existing_file(root, &parts, hub) {
        bail!("missing or unsafe model metadata: {relative}");
    }
    Ok(serde_json::from_slice(&std::fs::read(
        root.join(relative),
    )?)?)
}

fn validate_saved_prompt(config: &Value) -> Result<()> {
    if !config["default_prompt_name"].is_null() {
        bail!("deferred: saved default sentence-transformer prompt");
    }
    Ok(())
}

fn cache_root() -> PathBuf {
    cache_root_from(|key| std::env::var_os(key))
}

fn cache_root_from(lookup: impl Fn(&str) -> Option<std::ffi::OsString>) -> PathBuf {
    lookup("HF_HUB_CACHE")
        .or_else(|| lookup("HUGGINGFACE_HUB_CACHE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            lookup("HF_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    lookup("XDG_CACHE_HOME")
                        .map(PathBuf::from)
                        .unwrap_or_else(|| {
                            lookup(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
                                .map(PathBuf::from)
                                .unwrap_or_default()
                                .join(".cache")
                        })
                        .join("huggingface")
                })
                .join("hub")
        })
}

impl Embedder {
    /// An override is a complete local model root. The configured graph name
    /// is relative to its Transformer module; loading never guesses files.
    pub fn load_config(
        config: &EmbeddingConfig,
        override_root: Option<&Path>,
        threads: usize,
    ) -> Result<Self> {
        if !matches!(config.backend.as_str(), "torch" | "onnx") {
            bail!("embedding backend must be 'torch' or 'onnx'");
        }
        let requested = std::env::var("PSEUDOLIFE_EMBEDDING_CPU_DTYPE")
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        let dtype = if requested.is_empty() {
            config.cpu_dtype.trim().to_lowercase()
        } else {
            requested
        };
        if !matches!(dtype.as_str(), "auto" | "fp32" | "bf16") {
            bail!("embedding cpu_dtype must be 'auto', 'fp32' or 'bf16'");
        }
        // The port currently admits float32 hidden states only. An explicit
        // bf16 request must not silently become the measured fp32 policy.
        if dtype == "bf16" {
            bail!("deferred: bf16 ONNX");
        }
        if config.batch_size <= 0 || config.max_seq_length <= 0 {
            bail!("embedding batch_size and max_seq_length must be positive");
        }
        let file = config.onnx_file_name.replace('\\', "/");
        let model = override_root
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|| config.model_name.clone());
        let (root, hub) = artifacts::resolve(&model, &file, &cache_root())?;
        if root.join("config_sentence_transformers.json").exists() {
            validate_saved_prompt(&metadata(&root, "config_sentence_transformers.json", hub)?)?;
        }
        let mut transformer_path = String::new();
        let mut pooling_path = None;
        let mut always_normalize = false;
        if root.join("modules.json").exists() {
            let modules = metadata(&root, "modules.json", hub)?;
            let mut transformers = 0;
            for module in modules
                .as_array()
                .ok_or_else(|| anyhow!("invalid modules.json"))?
            {
                let kind = module["type"]
                    .as_str()
                    .ok_or_else(|| anyhow!("invalid module type"))?;
                let path = module["path"]
                    .as_str()
                    .ok_or_else(|| anyhow!("invalid module path"))?;
                if artifacts::transformer(kind) {
                    transformers += 1;
                    transformer_path = path.to_string();
                    if transformers > 1 || pooling_path.is_some() || always_normalize {
                        bail!("deferred: multiple or reordered Transformer modules");
                    }
                } else if kind.ends_with(".Pooling") {
                    if transformers != 1 || pooling_path.is_some() || always_normalize {
                        bail!("deferred: reordered Pooling module");
                    }
                    pooling_path = Some(path.to_string());
                } else if kind.ends_with(".Normalize") {
                    if pooling_path.is_none() || always_normalize {
                        bail!("deferred: reordered Normalize module");
                    }
                    always_normalize = true;
                } else {
                    bail!("deferred: Dense embedding postprocessor");
                }
            }
        }
        let sub = |name: &str| {
            if transformer_path.is_empty() {
                name.to_string()
            } else {
                format!("{transformer_path}/{name}")
            }
        };
        let model_config = metadata(&root, &sub("config.json"), hub)?;
        let dim = model_config["hidden_size"]
            .as_u64()
            .ok_or_else(|| anyhow!("model has no hidden_size"))? as usize;
        let expected = match config.model_name.as_str() {
            "Qwen/Qwen3-Embedding-0.6B" => Some(1024),
            "sentence-transformers/all-MiniLM-L6-v2" | "all-MiniLM-L6-v2" => Some(384),
            _ => None,
        };
        if dim == 0 || expected.is_some_and(|n| n != dim) {
            bail!("embedding dimension does not match configured model");
        }
        let pooling = if let Some(path) = pooling_path {
            let name = if path.is_empty() {
                "config.json".into()
            } else {
                format!("{path}/config.json")
            };
            let p = metadata(&root, &name, hub)?;
            if p["word_embedding_dimension"].as_u64() != Some(dim as u64) {
                bail!("pooling dimension mismatch");
            }
            let modes: Vec<_> = [
                "cls_token",
                "mean_tokens",
                "max_tokens",
                "mean_sqrt_len_tokens",
                "weightedmean_tokens",
                "lasttoken",
            ]
            .into_iter()
            .filter(|m| p[format!("pooling_mode_{m}")].as_bool() == Some(true))
            .collect();
            match modes.as_slice() {
                ["mean_tokens"] => Pooling::Mean,
                ["lasttoken"] => Pooling::Last,
                _ => bail!("deferred: unsupported embedding pooling mode"),
            }
        } else {
            Pooling::Mean
        };
        let st_name = sub("sentence_bert_config.json");
        let st = if root.join(&st_name).exists() {
            metadata(&root, &st_name, hub)?
        } else {
            Value::Null
        };
        let tokenizer_config = metadata(&root, &sub("tokenizer_config.json"), hub)?;
        let native_cap = st["max_seq_length"].as_u64().unwrap_or_else(|| {
            model_config["max_position_embeddings"]
                .as_u64()
                .unwrap_or(512)
                .min(
                    tokenizer_config["model_max_length"]
                        .as_u64()
                        .unwrap_or(u64::MAX),
                )
        });
        let cap = (config.max_seq_length as usize).min(native_cap as usize);
        let tokenizer_name = sub("tokenizer.json");
        if !artifacts::existing_file(&root, &artifacts::relative_parts(&tokenizer_name)?, hub) {
            bail!("missing or unsafe tokenizer.json");
        }
        let mut tokenizer =
            Tokenizer::from_file(root.join(&tokenizer_name)).map_err(|e| anyhow!("{e}"))?;
        tokenizer
            .with_truncation(Some(TruncationParams {
                max_length: if crate::mutants::active("embedding-no-truncation") {
                    native_cap as usize
                } else {
                    cap
                },
                direction: if tokenizer_config["truncation_side"] == "left" {
                    TruncationDirection::Left
                } else {
                    TruncationDirection::Right
                },
                ..Default::default()
            }))
            .map_err(|e| anyhow!("{e}"))?;
        let pad_token = tokenizer_config["pad_token"]
            .as_str()
            .or_else(|| tokenizer_config["pad_token"]["content"].as_str())
            .ok_or_else(|| anyhow!("tokenizer has no pad_token"))?;
        let pad_id = tokenizer
            .token_to_id(pad_token)
            .ok_or_else(|| anyhow!("unknown pad_token"))?;
        tokenizer.with_padding(Some(PaddingParams {
            direction: if tokenizer_config["padding_side"] == "left" {
                PaddingDirection::Left
            } else {
                PaddingDirection::Right
            },
            pad_id,
            pad_token: pad_token.to_string(),
            ..Default::default()
        }));
        let graph = root.join(sub(&file));
        let session = crate::onnx_runtime::load_cpu_session(&graph, threads)?;
        if session.inputs.iter().any(|i| {
            !matches!(
                i.name.as_str(),
                "input_ids" | "attention_mask" | "token_type_ids" | "position_ids"
            )
        }) {
            bail!("deferred: unsupported ONNX embedding input");
        }
        Ok(Self {
            config: config.clone(),
            tokenizer,
            state: Mutex::new(State {
                session,
                cache: VecDeque::new(),
                forwards: 0,
            }),
            pooling,
            always_normalize,
            lower_case: st["do_lower_case"].as_bool().unwrap_or(false),
            dim,
            model_root: root.clone(),
            graph,
            tokenizer_file: root.join(tokenizer_name),
        })
    }

    pub fn embedding_dim(&self) -> usize {
        self.dim
    }

    pub fn tokenize(&self, texts: &[String]) -> Result<Vec<tokenizers::Encoding>> {
        let cleaned: Vec<_> = texts
            .iter()
            .map(|s| {
                let s = s.trim_matches(|c: char| {
                    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
                });
                if self.lower_case {
                    s.to_lowercase()
                } else {
                    s.to_string()
                }
            })
            .collect();
        self.tokenizer
            .encode_batch(cleaned, true)
            .map_err(|e| anyhow!("{e}"))
    }

    fn forward(
        &self,
        state: &mut State,
        texts: &[String],
        normalize: bool,
    ) -> Result<Vec<Vec<f32>>> {
        let encodings = self.tokenize(texts)?;
        let t = encodings
            .first()
            .ok_or_else(|| anyhow!("empty batch"))?
            .len();
        if t == 0 {
            bail!("empty tokenization");
        }
        let shape = [texts.len(), t];
        let mut inputs = Vec::new();
        for input in &state.session.inputs {
            let data: Vec<i64> = match input.name.as_str() {
                "input_ids" => encodings
                    .iter()
                    .flat_map(|e| e.get_ids().iter().map(|v| *v as i64))
                    .collect(),
                "attention_mask" => encodings
                    .iter()
                    .flat_map(|e| e.get_attention_mask().iter().map(|v| *v as i64))
                    .collect(),
                "token_type_ids" => encodings
                    .iter()
                    .flat_map(|e| e.get_type_ids().iter().map(|v| *v as i64))
                    .collect(),
                "position_ids" => (0..texts.len()).flat_map(|_| 0..t as i64).collect(),
                _ => bail!("unsupported input"),
            };
            inputs.push((
                input.name.clone(),
                ort::session::SessionInputValue::from(Tensor::from_array((shape, data))?),
            ));
        }
        let outputs = state.session.run(inputs)?;
        let output = outputs
            .get("last_hidden_state")
            .ok_or_else(|| anyhow!("graph has no last_hidden_state"))?;
        let (dims, hidden) = output.try_extract_tensor::<f32>()?;
        if dims.as_ref() != [texts.len() as i64, t as i64, self.dim as i64] {
            bail!("ONNX hidden-state dimension mismatch");
        }
        let mut rows = Vec::new();
        for (i, encoding) in encodings.iter().enumerate() {
            let mode = if crate::mutants::active("embedding-wrong-pool") {
                Pooling::Last
            } else {
                self.pooling
            };
            let row_index = if crate::mutants::active("embedding-batch-row-offset") {
                (i + 1) % texts.len()
            } else {
                i
            };
            let mask_blind = (crate::mutants::active("embedding-mean-mask-blind")
                && matches!(mode, Pooling::Mean))
            .then(|| vec![1; t]);
            let mut row = embedding_math::pool(
                &hidden[row_index * t * self.dim..(row_index + 1) * t * self.dim],
                mask_blind
                    .as_deref()
                    .unwrap_or(encoding.get_attention_mask()),
                self.dim,
                mode,
            )?;
            if !crate::mutants::active("embedding-no-normalize") {
                if self.always_normalize {
                    embedding_math::normalize(&mut row);
                }
                if normalize {
                    embedding_math::normalize(&mut row);
                }
            }
            rows.push(row);
        }
        state.forwards += 1;
        Ok(rows)
    }

    pub fn encode(&self, texts: &[String], normalize: bool) -> Result<Vec<Vec<f32>>> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| anyhow!("embedding lock poisoned"))?;
        let capacity = if crate::mutants::active("embedding-cache-disabled") {
            0
        } else {
            self.config.cache_size.max(0) as usize
        };
        let mut rows = vec![None; texts.len()];
        let mut misses = Vec::new();
        for (i, text) in texts.iter().enumerate() {
            let key = (
                text.clone(),
                normalize && !crate::mutants::active("embedding-cache-key"),
            );
            if capacity > 0
                && let Some(index) = state.cache.iter().position(|(k, _)| *k == key)
            {
                let item = state.cache.remove(index).unwrap();
                rows[i] = Some(item.1.clone());
                state.cache.push_back(item);
            } else {
                misses.push(i);
            }
        }
        // SentenceTransformer sorts by unstripped Unicode text length before batching.
        let mut sorted = misses.clone();
        sorted.sort_by_key(|i| std::cmp::Reverse(texts[*i].chars().count()));
        for batch in sorted.chunks(self.config.batch_size as usize) {
            let fresh = self.forward(
                &mut state,
                &batch.iter().map(|i| texts[*i].clone()).collect::<Vec<_>>(),
                normalize,
            )?;
            for (i, row) in batch.iter().zip(fresh) {
                rows[*i] = Some(row);
            }
        }
        if capacity > 0 {
            for i in misses {
                let key = (
                    texts[i].clone(),
                    normalize && !crate::mutants::active("embedding-cache-key"),
                );
                if let Some(index) = state.cache.iter().position(|(k, _)| *k == key) {
                    state.cache.remove(index);
                }
                state
                    .cache
                    .push_back((key, rows[i].as_ref().unwrap().clone()));
            }
            while state.cache.len() > capacity {
                state.cache.pop_front();
            }
        }
        Ok(rows.into_iter().map(Option::unwrap).collect())
    }

    pub fn embed(&self, text: &str) -> Result<Vec<f32>> {
        Ok(self.encode(&[text.to_string()], true)?.remove(0))
    }
    pub fn embed_query(&self, text: &str) -> Result<Vec<f32>> {
        self.encode_query(text, true)
    }
    pub fn encode_query(&self, text: &str, normalize: bool) -> Result<Vec<f32>> {
        let prefix = if crate::mutants::active("embedding-no-prefix") {
            ""
        } else {
            &self.config.query_prefix
        };
        Ok(self
            .encode(&[format!("{prefix}{text}")], normalize)?
            .remove(0))
    }
    pub fn forwards(&self) -> Result<usize> {
        Ok(self
            .state
            .lock()
            .map_err(|_| anyhow!("embedding lock poisoned"))?
            .forwards)
    }
}

/// Private process boundary for the differential harness. Float32 bit patterns
/// avoid any JSON float printer or parser changing the vectors being compared.
pub fn probe() -> Result<()> {
    use std::io::Read;
    let mut raw = String::new();
    std::io::stdin().read_to_string(&mut raw)?;
    let request: Value = serde_json::from_str(&raw)?;
    let config_path = request["config_path"]
        .as_str()
        .ok_or_else(|| anyhow!("missing config_path"))?;
    let config = crate::config::load(Path::new(config_path))
        .map_err(|e| anyhow!("{e}"))?
        .embedding;
    let root = std::env::var_os("PSEUDOLIFE_DAEMON_ONNX_DIR").map(PathBuf::from);
    let model = Embedder::load_config(&config, root.as_deref(), 3)?;
    let mut results = Vec::new();
    for op in request["operations"]
        .as_array()
        .ok_or_else(|| anyhow!("missing operations"))?
    {
        let texts: Vec<String> = op["texts"]
            .as_array()
            .ok_or_else(|| anyhow!("missing texts"))?
            .iter()
            .map(|s| {
                s.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| anyhow!("non-string text"))
            })
            .collect::<Result<_>>()?;
        let norm = op["normalize"].as_bool().unwrap_or(true);
        let query = op["query"].as_bool().unwrap_or(false);
        let rows = if query {
            texts
                .iter()
                .map(|text| model.encode_query(text, norm))
                .collect::<Result<Vec<_>>>()?
        } else {
            model.encode(&texts, norm)?
        };
        let token_texts: Vec<_> = texts
            .iter()
            .map(|s| {
                if query {
                    format!(
                        "{}{s}",
                        if crate::mutants::active("embedding-no-prefix") {
                            ""
                        } else {
                            &config.query_prefix
                        }
                    )
                } else {
                    s.clone()
                }
            })
            .collect();
        let tokens = if token_texts.is_empty() {
            vec![]
        } else {
            model.tokenize(&token_texts)?
        };
        results.push(serde_json::json!({
            "bits": rows.iter().map(|row| row.iter().map(|v| v.to_bits()).collect::<Vec<_>>()).collect::<Vec<_>>(),
            "tokens": tokens.iter().map(|e| serde_json::json!({"ids": e.get_ids(), "mask": e.get_attention_mask()})).collect::<Vec<_>>(),
            "forwards": model.forwards()?,
        }));
    }
    // Consume returned storage independently: it never aliases the LRU.
    let mut first = model.embed("cache copy control")?;
    let expected = first.clone();
    first.fill(99.);
    let copy_safe = model.embed("cache copy control")? == expected;
    use sha2::{Digest, Sha256};
    let mut identities = serde_json::Map::new();
    let external = model.graph.with_file_name(format!(
        "{}_data",
        model.graph.file_name().unwrap().to_string_lossy()
    ));
    for file in [&model.graph, &model.tokenizer_file, &external] {
        if !file.is_file() {
            continue;
        }
        let mut stream = std::fs::File::open(file)?;
        let mut hash = Sha256::new();
        let mut buffer = vec![0u8; 1024 * 1024];
        loop {
            let n = stream.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            hash.update(&buffer[..n]);
        }
        identities.insert(
            file.strip_prefix(&model.model_root)?
                .to_string_lossy()
                .replace('\\', "/"),
            Value::String(hex::encode(hash.finalize())),
        );
    }
    println!(
        "{}",
        serde_json::json!({"dim": model.embedding_dim(), "operations": results,
        "copy_safe": copy_safe, "model_root": model.model_root, "graph": model.graph, "identities": identities})
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saved_default_prompts_are_explicitly_deferred() {
        assert!(
            validate_saved_prompt(
                &serde_json::json!({"prompts": {"query": "query "}, "default_prompt_name": "query"})
            )
            .is_err()
        );
        assert!(
            validate_saved_prompt(
                &serde_json::json!({"prompts": {"query": "query "}, "default_prompt_name": null})
            )
            .is_ok()
        );
    }

    #[test]
    fn hub_cache_uses_xdg_and_respects_explicit_overrides() {
        let xdg = |key: &str| match key {
            "XDG_CACHE_HOME" => Some("fixture-cache".into()),
            "HOME" | "USERPROFILE" => Some("fixture-home".into()),
            _ => None,
        };
        assert_eq!(
            cache_root_from(xdg),
            PathBuf::from("fixture-cache/huggingface/hub")
        );
        assert_eq!(
            cache_root_from(|key| if key == "HF_HOME" {
                Some("hf-home".into())
            } else {
                xdg(key)
            }),
            PathBuf::from("hf-home/hub")
        );
        assert_eq!(
            cache_root_from(|key| if key == "HF_HUB_CACHE" {
                Some("hub-override".into())
            } else {
                xdg(key)
            }),
            PathBuf::from("hub-override")
        );
    }
}
