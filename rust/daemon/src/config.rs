//! Configuration: config.yaml plus environment (spec section C).
//!
//! The oracle is `pseudolife_memory/utils/config.py::load_config` as the
//! daemon runs it: `MemoryService.__init__` loads the file, writes through
//! `memory.reference`, then applies the MCP overlay (`service.py:844-864`,
//! `_apply_mcp_defaults`). Every refusal the Python daemon raises while doing
//! that is a refusal here. Keys this module reads that Python accepts with
//! any type are type-checked here instead (a declared divergence: Python
//! starts and fails later).

// Wired by main in W1-A.
#![allow(dead_code)]

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::{Map as JsonMap, Value};
use yaml_rust2::Yaml;
use yaml_rust2::parser::{Event, MarkedEventReceiver, Parser, Tag};
use yaml_rust2::scanner::{Marker, TScalarStyle};

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub memory: MemoryConfig,
    pub embedding: EmbeddingConfig,
    pub coordination: CoordinationConfig,
    pub updates: UpdatesConfig,
    pub dream: DreamConfig,
}

#[derive(Debug, Clone, PartialEq)]
pub struct MemoryConfig {
    pub top_k: i64,
    pub hide_superseded: bool,
    pub search_confidence_floor: f64,
    pub recency_boost_enabled: bool,
    pub recency_base_half_life_s: f64,
    pub preset: String,
    pub bands: Vec<String>,
    pub search: SearchConfig,
    pub bm25: Bm25Config,
    pub reranker_enabled: bool,
    /// `memory.retrieval_log.enabled` (default on): with dreaming, it decides
    /// whether the sweep thread starts and parses its interval.
    pub retrieval_log_enabled: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SearchConfig {
    pub min_score: f64,
    pub fusion: String,
    pub candidate_pool_multiplier: i64,
    pub contiguity_neighbors: i64,
    pub timeline_channel: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Bm25Config {
    pub enabled: bool,
    pub k1: f64,
    pub b: f64,
    pub weight: f64,
    pub top_n: i64,
    pub min_score: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct EmbeddingConfig {
    pub model_name: String,
    pub device: String,
    pub backend: String,
    pub query_prefix: String,
    pub max_seq_length: i64,
    pub onnx_file_name: String,
    pub batch_size: i64,
    pub cache_size: i64,
    pub cpu_dtype: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CoordinationConfig {
    pub enabled: bool,
    pub wake: WakeConfig,
    /// Stripped, lower-cased; `["default"]` when the key is absent.
    pub allowed_principals: Vec<String>,
}

/// Field order is Python `WakeConfig`'s (`utils/config.py:1418-1423`).
#[derive(Debug, Clone, PartialEq)]
pub struct WakeConfig {
    pub per_recipient_per_hour: i64,
    pub urgent_per_sender_per_hour: i64,
    pub nightly_total: i64,
    pub fan_out_stagger_seconds: i64,
    pub active_seconds: i64,
    pub authority_per_sender_per_hour: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct UpdatesConfig {
    pub check_releases: bool,
    pub unattended_clients: bool,
    pub unattended_daemon: bool,
    pub check_interval_seconds: i64,
}

/// The `memory.dream` keys `resolve_endpoints` reads to decide whether an
/// extractor is configured (`memory/dream.py:1984-2038`).
#[derive(Debug, Clone, PartialEq)]
pub struct DreamConfig {
    pub enabled: bool,
    pub extractor_source: String,
    pub extractor_base_url: Option<String>,
    pub extractor_model: Option<String>,
    pub fallback_base_url: Option<String>,
    pub fallback_model: Option<String>,
    pub extractor_model_override: Option<String>,
    /// `float(sweep_interval_seconds)` succeeds (`mcp_server.py:3268`).
    pub sweep_interval_ok: bool,
}

impl Config {
    /// Every value this port reads, under its Python dotted path, for the
    /// harness's config differential (`PSEUDOLIFE_DAEMON_DUMP_CONFIG`).
    pub fn dump(&self) -> serde_json::Value {
        let m = &self.memory;
        let (s, b, e, c, u, d) = (
            &m.search,
            &m.bm25,
            &self.embedding,
            &self.coordination,
            &self.updates,
            &self.dream,
        );
        let mut dump = serde_json::json!({
            "memory.top_k": m.top_k,
            "memory.hide_superseded": m.hide_superseded,
            "memory.search_confidence_floor": m.search_confidence_floor,
            "memory.recency_boost_enabled": m.recency_boost_enabled,
            "memory.recency_base_half_life_s": m.recency_base_half_life_s,
            "memory.miras.preset": m.preset,
            "memory.miras.bands": m.bands,
            "memory.search.min_score": s.min_score,
            "memory.search.fusion": s.fusion,
            "memory.search.candidate_pool_multiplier": s.candidate_pool_multiplier,
            "memory.search.contiguity_neighbors": s.contiguity_neighbors,
            "memory.search.timeline_channel": s.timeline_channel,
            "memory.bm25.enabled": b.enabled,
            "memory.bm25.k1": b.k1,
            "memory.bm25.b": b.b,
            "memory.bm25.weight": b.weight,
            "memory.bm25.top_n": b.top_n,
            "memory.bm25.min_score": b.min_score,
            "memory.reranker.enabled": m.reranker_enabled,
            "memory.retrieval_log.enabled": m.retrieval_log_enabled,
            "embedding.model_name": e.model_name,
            "embedding.device": e.device,
            "embedding.query_prefix": e.query_prefix,
            "embedding.max_seq_length": e.max_seq_length,
            "coordination.enabled": c.enabled,
            "coordination.wake": c.wake.to_json(),
            "coordination.allowed_principals": c.allowed_principals,
            "updates.check_releases": u.check_releases,
            "updates.unattended_clients": u.unattended_clients,
            "updates.unattended_daemon": u.unattended_daemon,
            "updates.check_interval_seconds": u.check_interval_seconds,
            "memory.dream.enabled": d.enabled,
            "memory.dream.extractor_source": d.extractor_source,
            "memory.dream.extractor_base_url": d.extractor_base_url,
            "memory.dream.extractor_model": d.extractor_model,
            "memory.dream.fallback_base_url": d.fallback_base_url,
            "memory.dream.fallback_model": d.fallback_model,
            "memory.dream.extractor_model_override": d.extractor_model_override,
        });
        dump.as_object_mut().expect("configuration object").extend(
            serde_json::json!({
                "embedding.backend": e.backend,
                "embedding.onnx_file_name": e.onnx_file_name,
                "embedding.batch_size": e.batch_size,
                "embedding.cache_size": e.cache_size,
                "embedding.cpu_dtype": e.cpu_dtype,
            })
            .as_object()
            .expect("embedding object")
            .clone(),
        );
        dump
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    /// The daemon refuses to start (exit 1).
    Refused(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Refused(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for ConfigError {}

fn refuse(message: impl Into<String>) -> ConfigError {
    ConfigError::Refused(message.into())
}

#[derive(Debug, Clone, PartialEq)]
pub struct DaemonEnv {
    pub host: String,
    /// Python `int()` of the variable; the range is checked at bind.
    pub port: i64,
    pub token: Option<String>,
    pub tokens_raw: Option<String>,
    pub trust_bind: bool,
    pub database_url: Option<String>,
    pub release_check_disabled: bool,
    pub plugin_dir: Option<PathBuf>,
    pub build: Option<BuildStamp>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BuildStamp {
    pub git_sha: String,
    pub dirty: Option<bool>,
    pub built_at: String,
    pub source: String,
}

// ---------------------------------------------------------------------------
// Defaults (the Python dataclass defaults)
// ---------------------------------------------------------------------------

const DEFAULT_QUERY_PREFIX: &str =
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:";

/// `MemoryConfig.recency_base_half_life_s` (`utils/config.py`).
const LIBRARY_HALF_LIFE_S: f64 = 3600.0;
/// The MCP overlay's value (`service.py:_apply_mcp_defaults`).
const MCP_HALF_LIFE_S: f64 = 86400.0;

const CONTINUUM_BANDS: [&str; 8] = [
    "working", "micro", "instant", "fast", "medium", "slow", "archival", "forever",
];
const FUSION_MODES: [&str; 2] = ["weighted_sum", "rrf"];
const MEMORY_POLICY_VARIANTS: [&str; 3] = ["none", "compact", "full_separate_hook"];
const RETIRED_WAKE_KEYS: [&str; 1] = ["nudge_interval_seconds"];
const DEFAULT_PRINCIPAL: &str = "default";
const DAEMON_PRINCIPAL: &str = "daemon";
/// The sections `load_config` tests with `in raw`, in its order.
const SECTIONS: [&str; 8] = [
    "embedding",
    "memory",
    "context",
    "storage",
    "time",
    "coordination",
    "memory_policy",
    "updates",
];

impl Default for SearchConfig {
    fn default() -> Self {
        SearchConfig {
            min_score: 0.25,
            fusion: "weighted_sum".to_string(),
            candidate_pool_multiplier: 1,
            contiguity_neighbors: 0,
            timeline_channel: false,
        }
    }
}

impl Default for Bm25Config {
    fn default() -> Self {
        Bm25Config {
            enabled: true,
            k1: 1.5,
            b: 0.75,
            weight: 0.3,
            top_n: 20,
            min_score: 0.1,
        }
    }
}

impl Default for MemoryConfig {
    fn default() -> Self {
        MemoryConfig {
            top_k: 8,
            hide_superseded: false,
            search_confidence_floor: 0.0,
            recency_boost_enabled: false,
            recency_base_half_life_s: LIBRARY_HALF_LIFE_S,
            preset: "flat".to_string(),
            bands: vec!["flat".to_string()],
            search: SearchConfig::default(),
            bm25: Bm25Config::default(),
            reranker_enabled: false,
            retrieval_log_enabled: true,
        }
    }
}

impl Default for EmbeddingConfig {
    fn default() -> Self {
        EmbeddingConfig {
            model_name: "Qwen/Qwen3-Embedding-0.6B".to_string(),
            device: "cuda".to_string(),
            backend: "torch".to_string(),
            query_prefix: DEFAULT_QUERY_PREFIX.to_string(),
            max_seq_length: 512,
            onnx_file_name: "onnx/model.onnx".to_string(),
            batch_size: 16, // MemoryService MCP overlay, service.py:1106-1107
            cache_size: 1024,
            cpu_dtype: "auto".to_string(),
        }
    }
}

impl Default for WakeConfig {
    fn default() -> Self {
        WakeConfig {
            per_recipient_per_hour: 20,
            urgent_per_sender_per_hour: 6,
            nightly_total: 200,
            fan_out_stagger_seconds: 30,
            active_seconds: 60,
            authority_per_sender_per_hour: 12,
        }
    }
}

impl Default for CoordinationConfig {
    fn default() -> Self {
        CoordinationConfig {
            enabled: true,
            wake: WakeConfig::default(),
            allowed_principals: vec![DEFAULT_PRINCIPAL.to_string()],
        }
    }
}

impl Default for UpdatesConfig {
    fn default() -> Self {
        UpdatesConfig {
            check_releases: true,
            unattended_clients: false,
            unattended_daemon: false,
            check_interval_seconds: 6 * 3600,
        }
    }
}

impl Default for DreamConfig {
    fn default() -> Self {
        DreamConfig {
            enabled: true,
            extractor_source: "env".to_string(),
            extractor_base_url: None,
            extractor_model: None,
            fallback_base_url: None,
            fallback_model: None,
            extractor_model_override: None,
            sweep_interval_ok: true,
        }
    }
}

impl Default for Config {
    /// No config file: `AppConfig()` plus the MCP overlay.
    fn default() -> Self {
        Config {
            memory: MemoryConfig {
                recency_base_half_life_s: MCP_HALF_LIFE_S,
                ..MemoryConfig::default()
            },
            embedding: EmbeddingConfig::default(),
            coordination: CoordinationConfig::default(),
            updates: UpdatesConfig::default(),
            dream: DreamConfig::default(),
        }
    }
}

impl WakeConfig {
    const NAMES: [&'static str; 6] = [
        "per_recipient_per_hour",
        "urgent_per_sender_per_hour",
        "nightly_total",
        "fan_out_stagger_seconds",
        "active_seconds",
        "authority_per_sender_per_hour",
    ];

    fn values(&self) -> [i64; 6] {
        [
            self.per_recipient_per_hour,
            self.urgent_per_sender_per_hour,
            self.nightly_total,
            self.fan_out_stagger_seconds,
            self.active_seconds,
            self.authority_per_sender_per_hour,
        ]
    }

    /// `dataclasses.asdict(WakeConfig)`: keys in field order.
    pub fn to_json(&self) -> Value {
        let mut out = JsonMap::new();
        for (name, value) in Self::NAMES.iter().zip(self.values()) {
            out.insert((*name).to_string(), Value::from(value));
        }
        Value::Object(out)
    }
}

impl DreamConfig {
    /// `bool((primary_url and primary_model) or (fallback_url and
    /// fallback_model))` over `resolve_endpoints(cfg)`: with
    /// `extractor_source == "config"` the config values alone; otherwise each
    /// `PSEUDOLIFE_DREAM_*` variable wins when non-empty. A non-empty
    /// `extractor_model_override` replaces the primary model in both modes.
    pub fn extractor_configured(&self, env: &dyn Fn(&str) -> Option<String>) -> bool {
        let from_config = self.extractor_source == "config";
        let pick = |var: &str, configured: &Option<String>| -> Option<String> {
            if !from_config && let Some(value) = env(var).filter(|v| !v.is_empty()) {
                return Some(value);
            }
            configured.clone()
        };
        let primary_url = pick("PSEUDOLIFE_DREAM_BASE_URL", &self.extractor_base_url);
        let mut primary_model = pick("PSEUDOLIFE_DREAM_MODEL", &self.extractor_model);
        let fallback_url = pick(
            "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL",
            &self.fallback_base_url,
        );
        let fallback_model = pick("PSEUDOLIFE_DREAM_FALLBACK_MODEL", &self.fallback_model);
        if let Some(override_model) = self
            .extractor_model_override
            .as_ref()
            .filter(|v| !v.is_empty())
        {
            primary_model = Some(override_model.clone());
        }
        let truthy = |v: &Option<String>| v.as_deref().is_some_and(|s| !s.is_empty());
        (truthy(&primary_url) && truthy(&primary_model))
            || (truthy(&fallback_url) && truthy(&fallback_model))
    }
}

// ---------------------------------------------------------------------------
// Paths and environment
// ---------------------------------------------------------------------------

/// `(data_dir, config file)`: `PSEUDOLIFE_MCP_DATA_DIR` when non-empty, else
/// `<cwd>/data` (`service.py:844`); `PSEUDOLIFE_MCP_CONFIG` when set, even
/// empty, else `<data_dir>/config.yaml` (`service.py:847-852`,
/// `mcp_server.py:109-111`). An empty `PSEUDOLIFE_MCP_CONFIG` is Python's
/// `Path("")`, which is `.`: a directory, so [`load`] refuses it. The caller
/// creates `data_dir`.
pub fn config_path(env: &dyn Fn(&str) -> Option<String>, cwd: &Path) -> (PathBuf, PathBuf) {
    let data_dir = match env("PSEUDOLIFE_MCP_DATA_DIR") {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => cwd.join("data"),
    };
    let config = match env("PSEUDOLIFE_MCP_CONFIG") {
        Some(path) if path.is_empty() => PathBuf::from("."),
        Some(path) => PathBuf::from(path),
        None => data_dir.join("config.yaml"),
    };
    (data_dir, config)
}

/// Python's `str.isspace` set: Rust's `White_Space` plus U+001C..U+001F.
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

/// Python `int(text)` for base 10 (ASCII digits only): surrounding
/// whitespace, one sign, single underscores between digits.
fn py_int(text: &str) -> Option<i128> {
    let s = py_strip(text);
    let (negative, digits) = match s.as_bytes().first() {
        Some(b'+') => (false, &s[1..]),
        Some(b'-') => (true, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return None;
    }
    let mut value: i128 = 0;
    for c in digits.chars() {
        if c == '_' {
            continue;
        }
        let d = c.to_digit(10)?;
        value = value.checked_mul(10)?.checked_add(i128::from(d))?;
    }
    Some(if negative { -value } else { value })
}

impl DaemonEnv {
    /// The environment `run_daemon` reads (`daemon.py:525-559`), the build
    /// stamp (`daemon.py:_build_stamp`), the release-check off switch
    /// (`release_check.py:119`) and the plugin dir (`plugin_hooks.py:53`).
    /// A port Python's `int()` rejects refuses (exit 1); so does one outside
    /// 0..=65535, which Python only fails on at bind.
    pub fn from_env(env: &dyn Fn(&str) -> Option<String>) -> Result<Self, ConfigError> {
        let host = env("PSEUDOLIFE_MCP_HOST").unwrap_or_else(|| "127.0.0.1".to_string());
        let port = match env("PSEUDOLIFE_MCP_PORT") {
            None => 8765,
            Some(raw) => match py_int(&raw) {
                Some(value) => i64::try_from(value)
                    .map_err(|_| refuse(format!("PSEUDOLIFE_MCP_PORT {raw:?} is out of range")))?,
                None => {
                    return Err(refuse(format!(
                        "PSEUDOLIFE_MCP_PORT {raw:?} is not an integer"
                    )));
                }
            },
        };
        let non_empty = |name: &str| env(name).filter(|v| !v.is_empty());
        let trust_bind = matches!(
            env("PSEUDOLIFE_MCP_TRUST_BIND")
                .unwrap_or_default()
                .to_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on"
        );
        let release_check_disabled =
            env("PSEUDOLIFE_RELEASE_CHECK").is_some_and(|v| py_strip(&v) == "0");
        let build = non_empty("PSEUDOLIFE_BUILD_GIT_SHA").map(|git_sha| BuildStamp {
            git_sha,
            dirty: match py_strip(&env("PSEUDOLIFE_BUILD_DIRTY").unwrap_or_default())
                .to_lowercase()
                .as_str()
            {
                "true" => Some(true),
                "false" => Some(false),
                _ => None,
            },
            built_at: env("PSEUDOLIFE_BUILD_TIME").unwrap_or_else(|| "unknown".to_string()),
            source: non_empty("PSEUDOLIFE_BUILD_SOURCE").unwrap_or_else(|| "unknown".to_string()),
        });
        Ok(DaemonEnv {
            host,
            port,
            token: non_empty("PSEUDOLIFE_MCP_TOKEN"),
            tokens_raw: env("PSEUDOLIFE_MCP_TOKENS"),
            trust_bind,
            database_url: non_empty("PSEUDOLIFE_MCP_DATABASE_URL"),
            release_check_disabled,
            plugin_dir: non_empty("PSEUDOLIFE_PLUGIN_DIR").map(PathBuf::from),
            build,
        })
    }
}

// ---------------------------------------------------------------------------
// YAML: a PyYAML-shaped document tree over yaml-rust2's event parser
// ---------------------------------------------------------------------------

/// A loaded YAML node. Mappings keep their entries in first-insertion order
/// with the last duplicate's value (a Python dict built by PyYAML); merge
/// keys (`<<`) are applied when the mapping closes, as PyYAML's
/// `flatten_mapping` does.
#[derive(Debug, Clone, PartialEq)]
enum Node {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Seq(Vec<Node>),
    Map(Vec<(Node, Node)>),
    /// A plain, untagged `<<` key; only meaningful in key position.
    MergeKey,
}

impl Node {
    /// Python truthiness.
    fn truthy(&self) -> bool {
        match self {
            Node::Null => false,
            Node::Bool(b) => *b,
            Node::Int(i) => *i != 0,
            Node::Float(f) => *f != 0.0,
            Node::Str(s) => !s.is_empty(),
            Node::Seq(items) => !items.is_empty(),
            Node::Map(entries) => !entries.is_empty(),
            Node::MergeKey => true,
        }
    }

    fn type_name(&self) -> &'static str {
        match self {
            Node::Null => "null",
            Node::Bool(_) => "a boolean",
            Node::Int(_) => "an integer",
            Node::Float(_) => "a float",
            Node::Str(_) | Node::MergeKey => "a string",
            Node::Seq(_) => "a list",
            Node::Map(_) => "a mapping",
        }
    }
}

type Entries = Vec<(Node, Node)>;

fn map_insert(entries: &mut Entries, key: Node, value: Node) {
    if let Some(slot) = entries.iter_mut().find(|(k, _)| *k == key) {
        slot.1 = value;
    } else {
        entries.push((key, value));
    }
}

fn lookup<'a>(entries: &'a [(Node, Node)], key: &str) -> Option<&'a Node> {
    entries.iter().find_map(|(k, v)| match k {
        Node::Str(s) if s == key => Some(v),
        _ => None,
    })
}

/// yaml-rust2's float reading (`yaml.rs::parse_f64`).
fn parse_f64(v: &str) -> Option<f64> {
    match v {
        ".inf" | ".Inf" | ".INF" | "+.inf" | "+.Inf" | "+.INF" => Some(f64::INFINITY),
        "-.inf" | "-.Inf" | "-.INF" => Some(f64::NEG_INFINITY),
        ".nan" | ".NaN" | ".NAN" => Some(f64::NAN),
        _ if v.as_bytes().iter().any(u8::is_ascii_digit) => v.parse::<f64>().ok(),
        _ => None,
    }
}

const CORE_TAG: &str = "tag:yaml.org,2002:";

enum Frame {
    Seq(Vec<Node>, usize),
    /// Raw pairs (duplicates kept until the mapping closes) and the pending
    /// key.
    Map(Vec<(Node, Node)>, Option<Node>, usize),
}

#[derive(Default)]
struct Loader {
    docs: Vec<Node>,
    root: Option<Node>,
    stack: Vec<Frame>,
    anchors: HashMap<usize, Node>,
    error: Option<String>,
}

fn unknown_tag(tag: &Tag) -> String {
    format!(
        "could not determine a constructor for the tag '{}{}'",
        tag.handle, tag.suffix
    )
}

fn resolve_scalar(value: String, style: TScalarStyle, tag: Option<Tag>) -> Result<Node, String> {
    if let Some(tag) = tag {
        if tag.handle == "!" && tag.suffix.is_empty() {
            return Ok(Node::Str(value));
        }
        if tag.handle != CORE_TAG {
            return Err(unknown_tag(&tag));
        }
        let bad = || format!("cannot read {value:?} as !!{}", tag.suffix);
        return match tag.suffix.as_str() {
            "bool" => match value.as_str() {
                "true" | "True" | "TRUE" => Ok(Node::Bool(true)),
                "false" | "False" | "FALSE" => Ok(Node::Bool(false)),
                _ => Err(bad()),
            },
            "int" => value.parse::<i64>().map(Node::Int).map_err(|_| bad()),
            "float" => parse_f64(&value).map(Node::Float).ok_or_else(bad),
            "null" => match value.as_str() {
                "~" | "null" | "" => Ok(Node::Null),
                _ => Err(bad()),
            },
            "str" | "binary" | "timestamp" => Ok(Node::Str(value)),
            _ => Err(unknown_tag(&tag)),
        };
    }
    if style != TScalarStyle::Plain {
        return Ok(Node::Str(value));
    }
    Ok(match Yaml::from_str(&value) {
        Yaml::Null => Node::Null,
        Yaml::Boolean(b) => Node::Bool(b),
        Yaml::Integer(i) => Node::Int(i),
        Yaml::Real(raw) => match parse_f64(&raw) {
            Some(f) => Node::Float(f),
            None => Node::Str(raw),
        },
        _ if value == "<<" => Node::MergeKey,
        _ => Node::Str(value),
    })
}

/// SafeLoader constructs a tagged collection only as itself: `!!seq` on a
/// sequence, `!!map` on a mapping. Any other tag is a `ConstructorError`
/// (`!!str {}`, `!!foo []`, `!!map [1]`); `!!set`, `!!omap` and `!!pairs`
/// construct Python types no config key accepts, and are refused too.
fn check_collection_tag(tag: Option<Tag>, kind: &str) -> Result<(), String> {
    match tag {
        Some(tag) if tag.handle != CORE_TAG || tag.suffix != kind => Err(unknown_tag(&tag)),
        _ => Ok(()),
    }
}

/// PyYAML `flatten_mapping` + `construct_mapping`: merged entries first
/// (a later `<<` over an earlier one; within a `<<` list, the earlier
/// mapping wins), then the explicit entries, last duplicate winning.
fn finish_mapping(raw: Vec<(Node, Node)>) -> Result<Node, String> {
    let mut merged: Entries = Vec::new();
    let mut explicit: Vec<(Node, Node)> = Vec::new();
    for (key, value) in raw {
        if key != Node::MergeKey {
            explicit.push((key, value));
            continue;
        }
        match value {
            Node::Map(entries) => {
                for (k, v) in entries {
                    map_insert(&mut merged, k, v);
                }
            }
            Node::Seq(items) => {
                for item in items.into_iter().rev() {
                    match item {
                        Node::Map(entries) => {
                            for (k, v) in entries {
                                map_insert(&mut merged, k, v);
                            }
                        }
                        other => {
                            return Err(format!(
                                "expected a mapping for merging, but found {}",
                                other.type_name()
                            ));
                        }
                    }
                }
            }
            other => {
                return Err(format!(
                    "expected a mapping or list of mappings for merging, but found {}",
                    other.type_name()
                ));
            }
        }
    }
    for (key, value) in explicit {
        let key = if key == Node::MergeKey {
            Node::Str("<<".into())
        } else {
            key
        };
        map_insert(&mut merged, key, value);
    }
    Ok(Node::Map(merged))
}

impl Loader {
    fn complete(&mut self, node: Node, anchor: usize) {
        if anchor > 0 {
            self.anchors.insert(anchor, node.clone());
        }
        match self.stack.last_mut() {
            None => self.root = Some(node),
            Some(Frame::Seq(items, _)) => items.push(if node == Node::MergeKey {
                Node::Str("<<".into())
            } else {
                node
            }),
            Some(Frame::Map(pairs, pending, _)) => match pending.take() {
                None => *pending = Some(node),
                Some(key) => {
                    let value = if node == Node::MergeKey {
                        Node::Str("<<".into())
                    } else {
                        node
                    };
                    pairs.push((key, value));
                }
            },
        }
    }

    fn handle(&mut self, ev: Event) -> Result<(), String> {
        match ev {
            Event::Nothing | Event::StreamStart | Event::StreamEnd | Event::DocumentStart => {}
            Event::DocumentEnd => {
                let root = self.root.take().unwrap_or(Node::Null);
                self.docs.push(root);
            }
            Event::Alias(id) => {
                let node = self
                    .anchors
                    .get(&id)
                    .cloned()
                    .ok_or_else(|| "found undefined alias".to_string())?;
                self.complete(node, 0);
            }
            Event::Scalar(value, style, anchor, tag) => {
                let node = resolve_scalar(value, style, tag)?;
                self.complete(node, anchor);
            }
            Event::SequenceStart(anchor, tag) => {
                check_collection_tag(tag, "seq")?;
                self.stack.push(Frame::Seq(Vec::new(), anchor));
            }
            Event::SequenceEnd => {
                if let Some(Frame::Seq(items, anchor)) = self.stack.pop() {
                    self.complete(Node::Seq(items), anchor);
                }
            }
            Event::MappingStart(anchor, tag) => {
                check_collection_tag(tag, "map")?;
                self.stack.push(Frame::Map(Vec::new(), None, anchor));
            }
            Event::MappingEnd => {
                if let Some(Frame::Map(pairs, _, anchor)) = self.stack.pop() {
                    // A collection key is unhashable for construct_mapping.
                    if pairs
                        .iter()
                        .any(|(k, _)| matches!(k, Node::Map(_) | Node::Seq(_)))
                    {
                        return Err("found unhashable key while constructing a mapping".into());
                    }
                    let node = finish_mapping(pairs)?;
                    self.complete(node, anchor);
                }
            }
        }
        Ok(())
    }
}

impl MarkedEventReceiver for Loader {
    fn on_event(&mut self, ev: Event, _mark: Marker) {
        if self.error.is_none()
            && let Err(e) = self.handle(ev)
        {
            self.error = Some(e);
        }
    }
}

/// PyYAML's reader refuses these anywhere in the stream (`reader.py`
/// `NON_PRINTABLE`).
fn non_printable(c: char) -> bool {
    !matches!(c,
        '\u{09}' | '\u{0A}' | '\u{0D}' | '\u{20}'..='\u{7E}' | '\u{85}'
        | '\u{A0}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..='\u{10FFFF}')
}

/// `yaml.safe_load(text)`: `None` for an empty stream, refused on a syntax
/// error, more than one document, an unknown tag or a non-printable
/// character.
fn safe_load(text: &str) -> Result<Option<Node>, String> {
    if let Some((offset, c)) = text.char_indices().find(|(_, c)| non_printable(*c)) {
        return Err(format!(
            "unacceptable character #x{:04x} at offset {offset}",
            c as u32
        ));
    }
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut loader = Loader::default();
    let mut parser = Parser::new_from_str(text);
    parser.load(&mut loader, true).map_err(|e| e.to_string())?;
    if let Some(e) = loader.error {
        return Err(e);
    }
    let mut docs = loader.docs;
    match docs.len() {
        0 => Ok(None),
        1 => Ok(docs.pop()),
        _ => Err("expected a single document in the stream".to_string()),
    }
}

/// `_user_yaml_leaves`: dotted leaf keys of the document (string keys only;
/// no other key type can spell a key the overlay tests).
fn yaml_leaves(node: &Node) -> HashSet<String> {
    fn walk(node: &Node, prefix: &str, out: &mut HashSet<String>) {
        match node {
            Node::Map(entries) if !entries.is_empty() => {
                for (k, v) in entries {
                    if let Node::Str(k) = k {
                        walk(v, &format!("{prefix}{k}."), out);
                    }
                }
            }
            _ if !prefix.is_empty() => {
                out.insert(prefix[..prefix.len() - 1].to_string());
            }
            _ => {}
        }
    }
    let mut out = HashSet::new();
    walk(node, "", &mut out);
    out
}

// ---------------------------------------------------------------------------
// Typed reads
// ---------------------------------------------------------------------------

fn wrong_type(path: &str, want: &str, got: &Node) -> ConfigError {
    refuse(format!("{path} must be {want}, got {}", got.type_name()))
}

fn want_bool(
    entries: &[(Node, Node)],
    key: &str,
    path: &str,
    default: bool,
) -> Result<bool, ConfigError> {
    match lookup(entries, key) {
        None => Ok(default),
        Some(Node::Bool(b)) => Ok(*b),
        Some(other) => Err(wrong_type(&format!("{path}.{key}"), "a boolean", other)),
    }
}

fn want_int(
    entries: &[(Node, Node)],
    key: &str,
    path: &str,
    default: i64,
) -> Result<i64, ConfigError> {
    match lookup(entries, key) {
        None => Ok(default),
        Some(Node::Int(i)) => Ok(*i),
        Some(other) => Err(wrong_type(&format!("{path}.{key}"), "an integer", other)),
    }
}

fn node_f64(node: &Node) -> Option<f64> {
    match node {
        Node::Int(i) => Some(*i as f64),
        Node::Float(f) => Some(*f),
        _ => None,
    }
}

fn want_float(
    entries: &[(Node, Node)],
    key: &str,
    path: &str,
    default: f64,
) -> Result<f64, ConfigError> {
    match lookup(entries, key) {
        None => Ok(default),
        Some(node) => {
            node_f64(node).ok_or_else(|| wrong_type(&format!("{path}.{key}"), "a number", node))
        }
    }
}

fn want_str(
    entries: &[(Node, Node)],
    key: &str,
    path: &str,
    default: &str,
) -> Result<String, ConfigError> {
    match lookup(entries, key) {
        None => Ok(default.to_string()),
        Some(Node::Str(s)) => Ok(s.clone()),
        Some(other) => Err(wrong_type(&format!("{path}.{key}"), "a string", other)),
    }
}

fn want_opt_str(
    entries: &[(Node, Node)],
    key: &str,
    path: &str,
) -> Result<Option<String>, ConfigError> {
    match lookup(entries, key) {
        None | Some(Node::Null) => Ok(None),
        Some(Node::Str(s)) => Ok(Some(s.clone())),
        Some(other) => Err(wrong_type(
            &format!("{path}.{key}"),
            "a string or null",
            other,
        )),
    }
}

/// A section this module reads: absent is `None`; present but not a mapping
/// refuses (exact where Python refuses, otherwise a declared divergence).
fn section<'a>(
    entries: &'a [(Node, Node)],
    key: &str,
    path: &str,
) -> Result<Option<&'a [(Node, Node)]>, ConfigError> {
    match lookup(entries, key) {
        None => Ok(None),
        Some(Node::Map(inner)) => Ok(Some(inner)),
        Some(other) => Err(wrong_type(&format!("{path}{key}"), "a mapping", other)),
    }
}

/// Python `type(value) is int and value >= floor`.
fn exact_int_at_least(node: &Node, floor: i64) -> Option<i64> {
    match node {
        Node::Int(i) if *i >= floor => Some(*i),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// load
// ---------------------------------------------------------------------------

/// `load_config(path)` plus what `MemoryService.__init__` does with it before
/// serving: the `memory.reference` write and the MCP overlay.
pub fn load(path: &Path) -> Result<Config, ConfigError> {
    let path = if path.as_os_str().is_empty() {
        Path::new(".")
    } else {
        path
    };
    match std::fs::metadata(path) {
        Ok(_) => {}
        Err(e)
            if matches!(
                e.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            return Ok(Config::default());
        }
        Err(e) => return Err(refuse(format!("cannot read {}: {e}", path.display()))),
    }
    let bytes =
        std::fs::read(path).map_err(|e| refuse(format!("cannot read {}: {e}", path.display())))?;
    let text = String::from_utf8(bytes)
        .map_err(|e| refuse(format!("{} is not UTF-8: {e}", path.display())))?;
    load_str(&text)
}

fn load_str(text: &str) -> Result<Config, ConfigError> {
    let doc = safe_load(text).map_err(|e| refuse(format!("config.yaml: {e}")))?;
    // `yaml.safe_load(f) or {}`.
    let raw = match doc {
        Some(node) if node.truthy() => node,
        _ => Node::Map(Vec::new()),
    };
    let entries: &[(Node, Node)] = match &raw {
        Node::Map(entries) => entries,
        // `"embedding" in raw` on a list compares elements; a hit indexes the
        // list with a string (TypeError).
        Node::Seq(items) => {
            if items
                .iter()
                .any(|item| matches!(item, Node::Str(s) if SECTIONS.contains(&s.as_str())))
            {
                return Err(refuse(
                    "config.yaml: a top-level list cannot name a section",
                ));
            }
            return Ok(Config::default());
        }
        // On a string it is a substring test, and a hit indexes the string.
        Node::Str(s) => {
            if SECTIONS.iter().any(|name| s.contains(name)) {
                return Err(refuse(
                    "config.yaml: the top level is a string, not a mapping",
                ));
            }
            return Ok(Config::default());
        }
        other => {
            return Err(refuse(format!(
                "config.yaml: the top level is {}, not a mapping",
                other.type_name()
            )));
        }
    };
    let leaves = yaml_leaves(&raw);
    let user_set = |key: &str| leaves.contains(key);

    let mut config = Config::default();

    // embedding: the overlay's `config.embedding.batch_size = 16` fails on
    // anything but a mapping (exact).
    if let Some(e) = section(entries, "embedding", "")? {
        let p = "embedding";
        let d = EmbeddingConfig::default();
        config.embedding = EmbeddingConfig {
            model_name: want_str(e, "model_name", p, &d.model_name)?,
            device: want_str(e, "device", p, &d.device)?,
            backend: want_str(e, "backend", p, &d.backend)?,
            query_prefix: want_str(e, "query_prefix", p, &d.query_prefix)?,
            max_seq_length: want_int(e, "max_seq_length", p, d.max_seq_length)?,
            onnx_file_name: want_str(e, "onnx_file_name", p, &d.onnx_file_name)?,
            batch_size: want_int(e, "batch_size", p, d.batch_size)?,
            cache_size: want_int(e, "cache_size", p, d.cache_size)?,
            cpu_dtype: want_str(e, "cpu_dtype", p, &d.cpu_dtype)?,
        };
    }

    let mut memory = MemoryConfig::default();
    let mut half_life = Node::Float(LIBRARY_HALF_LIFE_S);
    if let Some(m) = section(entries, "memory", "")? {
        let p = "memory";
        memory.top_k = want_int(m, "top_k", p, memory.top_k)?;
        memory.hide_superseded = want_bool(m, "hide_superseded", p, memory.hide_superseded)?;
        memory.search_confidence_floor = want_float(
            m,
            "search_confidence_floor",
            p,
            memory.search_confidence_floor,
        )?;
        memory.recency_boost_enabled =
            want_bool(m, "recency_boost_enabled", p, memory.recency_boost_enabled)?;
        if let Some(node) = lookup(m, "recency_base_half_life_s") {
            half_life = node.clone();
        }

        if let Some(miras) = section(m, "miras", "memory.")? {
            let (preset, bands) = read_miras(miras)?;
            memory.preset = preset;
            memory.bands = bands;
        }
        // `self.config.memory.reference.persist_dir = ...` (service.py:857).
        section(m, "reference", "memory.")?;
        if let Some(s) = section(m, "search", "memory.")? {
            memory.search = read_search(s)?;
        }
        if let Some(b) = section(m, "bm25", "memory.")? {
            let p = "memory.bm25";
            let d = Bm25Config::default();
            memory.bm25 = Bm25Config {
                enabled: want_bool(b, "enabled", p, d.enabled)?,
                k1: want_float(b, "k1", p, d.k1)?,
                b: want_float(b, "b", p, d.b)?,
                weight: want_float(b, "weight", p, d.weight)?,
                top_n: want_int(b, "top_n", p, d.top_n)?,
                min_score: want_float(b, "min_score", p, d.min_score)?,
            };
        }
        if let Some(r) = section(m, "retrieval_log", "memory.")? {
            memory.retrieval_log_enabled = want_bool(r, "enabled", "memory.retrieval_log", true)?;
        }
        if let Some(r) = section(m, "reranker", "memory.")? {
            memory.reranker_enabled = want_bool(r, "enabled", "memory.reranker", false)?;
        }
        if let Some(d) = section(m, "dream", "memory.")? {
            config.dream = read_dream(d)?;
        }
        // The overlay writes `meta_filter.enabled` and
        // `traces.retention_boost` unless the user set them.
        for (key, leaf) in [
            ("meta_filter", "memory.meta_filter.enabled"),
            ("traces", "memory.traces.retention_boost"),
        ] {
            if let Some(node) = lookup(m, key)
                && !matches!(node, Node::Map(_))
                && !user_set(leaf)
            {
                return Err(wrong_type(&format!("memory.{key}"), "a mapping", node));
            }
        }
    }
    // The overlay: 86400 s unless the user set the leaf (service.py:1100).
    memory.recency_base_half_life_s = if user_set("memory.recency_base_half_life_s") {
        node_f64(&half_life)
            .ok_or_else(|| wrong_type("memory.recency_base_half_life_s", "a number", &half_life))?
    } else {
        MCP_HALF_LIFE_S
    };
    config.memory = memory;

    if let Some(c) = section(entries, "coordination", "")? {
        config.coordination = read_coordination(c)?;
    }
    // Validated only; a non-mapping is kept unvalidated by Python (exact).
    if let Some(Node::Map(policy)) = lookup(entries, "memory_policy") {
        validate_memory_policy(policy)?;
    }
    if let Some(u) = section(entries, "updates", "")? {
        config.updates = read_updates(u)?;
    }
    Ok(config)
}

fn read_miras(miras: &[(Node, Node)]) -> Result<(String, Vec<String>), ConfigError> {
    let preset = match lookup(miras, "preset") {
        None => "flat".to_string(),
        Some(Node::Str(s)) => s.clone(),
        Some(other) => {
            return Err(refuse(format!(
                "Unknown MIRAS preset ({}); expected flat, continuum, titans, moneta, yaad, memora or custom",
                other.type_name()
            )));
        }
    };
    // `miras_raw.get("bands", []) or []`, then iterated.
    let bands_raw = lookup(miras, "bands").filter(|n| n.truthy());
    let items: Vec<Node> = match bands_raw {
        None => Vec::new(),
        Some(Node::Seq(items)) => items.clone(),
        Some(Node::Str(s)) => s.chars().map(|c| Node::Str(c.to_string())).collect(),
        Some(Node::Map(entries)) => entries.iter().map(|(k, _)| k.clone()).collect(),
        Some(other) => {
            return Err(refuse(format!(
                "memory.miras.bands must be a list, got {}",
                other.type_name()
            )));
        }
    };
    let bands = match preset.as_str() {
        "flat" => vec!["flat".to_string()],
        "continuum" | "titans" | "moneta" | "yaad" | "memora" => {
            CONTINUUM_BANDS.iter().map(|s| (*s).to_string()).collect()
        }
        "custom" => {
            if items.is_empty() {
                return Err(refuse(
                    "MIRASConfig: preset='custom' requires a non-empty bands list in config.yaml",
                ));
            }
            let mut names = Vec::with_capacity(items.len());
            for item in &items {
                let Node::Map(spec) = item else {
                    return Err(wrong_type("memory.miras.bands[]", "a mapping", item));
                };
                names.push(want_str(spec, "name", "memory.miras.bands[]", "band")?);
            }
            names
        }
        other => {
            return Err(refuse(format!(
                "Unknown MIRAS preset {other:?}; expected flat, continuum, titans, moneta, yaad, memora or custom"
            )));
        }
    };
    Ok((preset, bands))
}

fn read_search(s: &[(Node, Node)]) -> Result<SearchConfig, ConfigError> {
    let p = "memory.search";
    let d = SearchConfig::default();
    let fusion = match lookup(s, "fusion") {
        None => d.fusion.clone(),
        Some(Node::Str(f)) if FUSION_MODES.contains(&f.as_str()) => f.clone(),
        Some(_) => {
            return Err(refuse(
                "memory.search.fusion: unknown mode (expected 'weighted_sum' or 'rrf')",
            ));
        }
    };
    let min_score = match lookup(s, "min_score") {
        None => d.min_score,
        Some(node) => match node_f64(node) {
            Some(v) if (0.0..=1.0).contains(&v) => v,
            _ => {
                return Err(refuse(
                    "memory.search.min_score: expected a cosine floor in [0, 1]",
                ));
            }
        },
    };
    Ok(SearchConfig {
        min_score,
        fusion,
        candidate_pool_multiplier: want_int(
            s,
            "candidate_pool_multiplier",
            p,
            d.candidate_pool_multiplier,
        )?,
        contiguity_neighbors: want_int(s, "contiguity_neighbors", p, d.contiguity_neighbors)?,
        timeline_channel: want_bool(s, "timeline_channel", p, d.timeline_channel)?,
    })
}

fn read_dream(d: &[(Node, Node)]) -> Result<DreamConfig, ConfigError> {
    let p = "memory.dream";
    let def = DreamConfig::default();
    let dream = DreamConfig {
        enabled: want_bool(d, "enabled", p, def.enabled)?,
        extractor_source: want_str(d, "extractor_source", p, &def.extractor_source)?,
        extractor_base_url: want_opt_str(d, "extractor_base_url", p)?,
        extractor_model: want_opt_str(d, "extractor_model", p)?,
        fallback_base_url: want_opt_str(d, "fallback_base_url", p)?,
        fallback_model: want_opt_str(d, "fallback_model", p)?,
        extractor_model_override: want_opt_str(d, "extractor_model_override", p)?,
        // Python `float()`: numbers, bools and numeric strings.
        sweep_interval_ok: match lookup(d, "sweep_interval_seconds") {
            None | Some(Node::Int(_) | Node::Float(_) | Node::Bool(_)) => true,
            Some(Node::Str(s)) => crate::storage::py_strip(s)
                .replace('_', "")
                .parse::<f64>()
                .is_ok(),
            Some(_) => false,
        },
    };
    // DreamConfig.__post_init__ (utils/config.py:503-509).
    if let Some(hours) = lookup(d, "stall_repeat_hours") {
        match node_f64(hours) {
            Some(h) if h.is_finite() && h > 0.0 => {}
            _ => {
                return Err(refuse(
                    "memory.dream.stall_repeat_hours must be a number of hours greater than 0",
                ));
            }
        }
    }
    Ok(dream)
}

/// A principal list: a YAML list of strings, each non-blank after Python's
/// `strip()`; returns the stripped, lower-cased names.
fn principal_list(c: &[(Node, Node)], key: &str) -> Result<Vec<String>, ConfigError> {
    let bad = || refuse(format!("coordination.{key} must be a list of names"));
    match lookup(c, key) {
        None => Ok(Vec::new()),
        Some(Node::Seq(items)) => items
            .iter()
            .map(|item| match item {
                Node::Str(s) if !py_strip(s).is_empty() => Ok(py_strip(s).to_lowercase()),
                _ => Err(bad()),
            })
            .collect(),
        Some(_) => Err(bad()),
    }
}

/// `CoordinationConfig.__post_init__` (utils/config.py:1552-1592), with the
/// wake and maintainer constructors it calls.
fn read_coordination(c: &[(Node, Node)]) -> Result<CoordinationConfig, ConfigError> {
    let enabled = match lookup(c, "enabled") {
        None => true,
        Some(Node::Bool(b)) => *b,
        Some(_) => return Err(refuse("coordination.enabled must be a boolean")),
    };
    match lookup(c, "maintainer") {
        None => {}
        Some(Node::Map(m)) => validate_maintainer(m)?,
        Some(_) => return Err(refuse("coordination.maintainer must be a mapping")),
    }
    let wake = match lookup(c, "wake") {
        None => WakeConfig::default(),
        Some(Node::Map(w)) => read_wake(w)?,
        Some(_) => return Err(refuse("coordination.wake must be a mapping of wake caps")),
    };
    if let Some(node) = lookup(c, "awareness_limit")
        && !matches!(node, Node::Int(i) if (1..=20).contains(i))
    {
        return Err(refuse(
            "coordination.awareness_limit must be an integer in 1..20",
        ));
    }
    if let Some(node) = lookup(c, "audit_retention_days")
        && exact_int_at_least(node, 0).is_none()
    {
        return Err(refuse(
            "coordination.audit_retention_days must be a whole number of days, 0 or more",
        ));
    }
    let allowed_principals = match lookup(c, "allowed_principals") {
        None => vec![DEFAULT_PRINCIPAL.to_string()],
        Some(_) => principal_list(c, "allowed_principals")?,
    };
    principal_list(c, "daemon_notice_principals")?;
    let maintainers = principal_list(c, "maintainer_principals")?;
    let mut shared: Vec<&str> = [DAEMON_PRINCIPAL, DEFAULT_PRINCIPAL]
        .into_iter()
        .filter(|p| maintainers.iter().any(|m| m == p))
        .collect();
    shared.sort_unstable();
    if !shared.is_empty() {
        return Err(refuse(format!(
            "coordination.maintainer_principals cannot name {}",
            shared.join(", ")
        )));
    }
    Ok(CoordinationConfig {
        enabled,
        wake,
        allowed_principals,
    })
}

/// `MaintainerConfig(**mapping)`: unknown or non-string keys are a
/// TypeError; then its `__post_init__`.
fn validate_maintainer(m: &[(Node, Node)]) -> Result<(), ConfigError> {
    const FIELDS: [&str; 3] = ["rp_id", "origin", "maintainer_per_recipient_per_hour"];
    for (key, _) in m {
        match key {
            Node::Str(k) if FIELDS.contains(&k.as_str()) => {}
            _ => return Err(refuse("coordination.maintainer: unexpected key")),
        }
    }
    for key in ["rp_id", "origin"] {
        if let Some(node) = lookup(m, key)
            && !matches!(node, Node::Str(_))
        {
            return Err(refuse(
                "coordination.maintainer.rp_id and .origin must be strings",
            ));
        }
    }
    if let Some(node) = lookup(m, "maintainer_per_recipient_per_hour")
        && exact_int_at_least(node, 0).is_none()
    {
        return Err(refuse(
            "coordination.maintainer.maintainer_per_recipient_per_hour must be a whole number, 0 or more",
        ));
    }
    Ok(())
}

/// `WakeConfig(**{k: v ... if k not in RETIRED_WAKE_KEYS})`: an unknown or
/// non-string key is a TypeError; then its `__post_init__`.
fn read_wake(w: &[(Node, Node)]) -> Result<WakeConfig, ConfigError> {
    for (key, _) in w {
        match key {
            Node::Str(k)
                if WakeConfig::NAMES.contains(&k.as_str())
                    || RETIRED_WAKE_KEYS.contains(&k.as_str()) => {}
            _ => return Err(refuse("coordination.wake: unexpected key")),
        }
    }
    let mut values = WakeConfig::default().values();
    for (slot, name) in values.iter_mut().zip(WakeConfig::NAMES) {
        let floor = if name == "active_seconds" { 1 } else { 0 };
        if let Some(node) = lookup(w, name) {
            *slot = exact_int_at_least(node, floor).ok_or_else(|| {
                refuse(format!(
                    "coordination.wake.{name} must be a whole number of at least {floor}"
                ))
            })?;
        }
    }
    let [a, b, c, d, e, f] = values;
    Ok(WakeConfig {
        per_recipient_per_hour: a,
        urgent_per_sender_per_hour: b,
        nightly_total: c,
        fan_out_stagger_seconds: d,
        active_seconds: e,
        authority_per_sender_per_hour: f,
    })
}

/// `MemoryPolicyConfig.__post_init__` (utils/config.py:1361-1374).
fn validate_memory_policy(p: &[(Node, Node)]) -> Result<(), ConfigError> {
    let is_variant =
        |n: &Node| matches!(n, Node::Str(s) if MEMORY_POLICY_VARIANTS.contains(&s.as_str()));
    if let Some(variant) = lookup(p, "variant")
        && !is_variant(variant)
    {
        return Err(refuse(
            "memory_policy.variant must be one of none, compact, full_separate_hook",
        ));
    }
    if let Some(arms) = lookup(p, "ab_arms") {
        let Node::Seq(items) = arms else {
            return Err(refuse("memory_policy.ab_arms must be a list of variants"));
        };
        if !items.iter().all(is_variant) {
            return Err(refuse("memory_policy.ab_arms must be a list of variants"));
        }
        if items.len() == 1 {
            return Err(refuse("memory_policy.ab_arms needs at least two arms"));
        }
    }
    Ok(())
}

/// `UpdatesConfig.__post_init__` (utils/config.py:1628-1633).
fn read_updates(u: &[(Node, Node)]) -> Result<UpdatesConfig, ConfigError> {
    let d = UpdatesConfig::default();
    let flag = |key: &str, default: bool| match lookup(u, key) {
        None => Ok(default),
        Some(Node::Bool(b)) => Ok(*b),
        Some(_) => Err(refuse(format!("updates.{key} must be a boolean"))),
    };
    let check_releases = flag("check_releases", d.check_releases)?;
    let unattended_clients = flag("unattended_clients", d.unattended_clients)?;
    let unattended_daemon = flag("unattended_daemon", d.unattended_daemon)?;
    let check_interval_seconds = match lookup(u, "check_interval_seconds") {
        None => d.check_interval_seconds,
        Some(node) => exact_int_at_least(node, 60).ok_or_else(|| {
            refuse("updates.check_interval_seconds must be a whole number of seconds, 60 or more")
        })?,
    };
    Ok(UpdatesConfig {
        check_releases,
        unattended_clients,
        unattended_daemon,
        check_interval_seconds,
    })
}

#[cfg(test)]
mod tests {
    //! Expected values come from the Python oracle: each YAML here was run
    //! through `MemoryService(data_dir=..., config_path=...)` at master
    //! 35b8f5d2 (load_config + the reference write + the MCP overlay).
    use super::*;

    fn cfg(text: &str) -> Config {
        match load_str(text) {
            Ok(c) => c,
            Err(e) => panic!("refused {text:?}: {e}"),
        }
    }

    fn refused(text: &str) -> bool {
        matches!(load_str(text), Err(ConfigError::Refused(_)))
    }

    #[test]
    fn collection_tags_and_unhashable_keys_refuse_like_safe_load() {
        // yaml.safe_load, probed 2026-10-09: ConstructorError for each.
        for text in [
            "!!foo {}\n",
            "other: !!str {}\n",
            "? [x]\n: 1\n",
            "? {a: 1}\n: 1\n",
            "!!map [1]\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
        for text in ["!!map {memory: {top_k: 3}}\n", "x: !!seq [1]\n"] {
            assert!(!refused(text), "{text:?}");
        }
    }

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |name: &str| {
            owned
                .iter()
                .find(|(k, _)| k == name)
                .map(|(_, v)| v.clone())
        }
    }

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("pl-config-test-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn defaults_match_the_dataclasses_plus_overlay() {
        let c = Config::default();
        assert_eq!(c.memory.top_k, 8);
        assert!(!c.memory.hide_superseded);
        assert_eq!(c.memory.search_confidence_floor, 0.0);
        assert!(!c.memory.recency_boost_enabled);
        assert_eq!(c.memory.recency_base_half_life_s, 86400.0);
        assert_eq!(c.memory.preset, "flat");
        assert_eq!(c.memory.bands, vec!["flat"]);
        assert_eq!(
            c.memory.search,
            SearchConfig {
                min_score: 0.25,
                fusion: "weighted_sum".into(),
                candidate_pool_multiplier: 1,
                contiguity_neighbors: 0,
                timeline_channel: false,
            }
        );
        assert_eq!(
            c.memory.bm25,
            Bm25Config {
                enabled: true,
                k1: 1.5,
                b: 0.75,
                weight: 0.3,
                top_n: 20,
                min_score: 0.1
            }
        );
        assert!(!c.memory.reranker_enabled);
        assert_eq!(c.embedding.model_name, "Qwen/Qwen3-Embedding-0.6B");
        assert_eq!(c.embedding.device, "cuda");
        assert_eq!(c.embedding.backend, "torch");
        assert_eq!(
            c.embedding.query_prefix,
            "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
        );
        assert_eq!(c.embedding.max_seq_length, 512);
        assert!(c.coordination.enabled);
        assert_eq!(
            c.updates,
            UpdatesConfig {
                check_releases: true,
                unattended_clients: false,
                unattended_daemon: false,
                check_interval_seconds: 21600,
            }
        );
        assert!(c.dream.enabled);
        assert!(!c.dream.extractor_configured(&env_of(&[])));
    }

    #[test]
    fn empty_and_falsy_documents_are_defaults() {
        for text in [
            "",
            "# hi\n",
            "---\n",
            "false\n",
            "0\n",
            "0.0\n",
            "[]\n",
            "{}\n",
            "''\n",
            "- a\n- b\n",
            "hello\n",
            "memory: {}\n",
            "memory:\n  nli:\n",
            "1: 2\n",
            "memory_policy:\n",
        ] {
            assert_eq!(cfg(text), Config::default(), "{text:?}");
        }
    }

    #[test]
    fn missing_file_is_defaults_and_unreadable_paths_refuse() {
        let dir = scratch("paths");
        assert_eq!(load(&dir.join("absent.yaml")).unwrap(), Config::default());
        // A directory (also what an empty PSEUDOLIFE_MCP_CONFIG becomes).
        assert!(load(&dir).is_err());
        assert!(load(Path::new("")).is_err());
        // Python reads the file as UTF-8 (the container locale) and refuses.
        let latin1 = dir.join("latin1.yaml");
        std::fs::write(&latin1, b"memory:\n  top_k: 4\n# caf\xe9\n").unwrap();
        assert!(load(&latin1).is_err());
        // A leading BOM is skipped, as PyYAML's scanner does.
        let bom = dir.join("bom.yaml");
        std::fs::write(&bom, b"\xef\xbb\xbfmemory:\n  top_k: 4\n").unwrap();
        assert_eq!(load(&bom).unwrap().memory.top_k, 4);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn top_level_shapes() {
        // `"memory" in raw` on a list compares elements, on a string tests
        // substrings; a hit indexes with a string (TypeError).
        assert!(refused("- memory\n"));
        assert!(refused("sometimes\n"));
        assert!(refused("mymemoryx\n"));
        assert!(refused("5\n"));
        assert!(refused("true\n"));
        assert!(refused(".nan\n"));
        assert!(refused("memory: [\n"));
        assert!(refused("a: 1\n---\nb: 2\n"));
        // PyYAML's reader rejects control characters anywhere.
        assert!(refused("# bell \u{7}\n"));
    }

    #[test]
    fn duplicate_keys_last_wins() {
        assert_eq!(cfg("memory:\n  top_k: 3\n  top_k: 5\n").memory.top_k, 5);
        let c = cfg("memory:\n  top_k: 3\nmemory:\n  hide_superseded: true\n");
        assert_eq!(c.memory.top_k, 8);
        assert!(c.memory.hide_superseded);
    }

    #[test]
    fn anchors_and_merge_keys() {
        assert_eq!(cfg("x: &a 7\nmemory:\n  top_k: *a\n").memory.top_k, 7);
        assert_eq!(
            cfg("base: &b\n  top_k: 9\nmemory:\n  <<: *b\n")
                .memory
                .top_k,
            9
        );
        // Explicit keys beat merged ones; within a list the first wins.
        let c = cfg(
            "a: &a {top_k: 1, hide_superseded: true}\nb: &b {top_k: 2}\n\
                     memory:\n  top_k: 3\n  <<: [*b, *a]\n",
        );
        assert_eq!(c.memory.top_k, 3);
        assert!(c.memory.hide_superseded);
        let c = cfg("a: &a {top_k: 1}\nb: &b {top_k: 2}\nmemory:\n  <<: [*b, *a]\n");
        assert_eq!(c.memory.top_k, 2);
        assert!(refused("memory:\n  <<: 5\n"));
    }

    #[test]
    fn unknown_tags_refuse_even_on_unread_keys() {
        assert!(refused("memory:\n  top_k: !foo 4\n"));
        assert!(refused("other: !foo 4\n"));
        assert!(refused("other: !!int abc\n"));
        assert_eq!(cfg("memory:\n  top_k: !!int 4\n").memory.top_k, 4);
    }

    #[test]
    fn memory_and_miras_must_be_mappings() {
        for text in [
            "memory:\n",
            "memory: [1]\n",
            "memory: x\n",
            "memory:\n  miras:\n",
            "memory:\n  miras: []\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
    }

    #[test]
    fn presets() {
        let eight = [
            "working", "micro", "instant", "fast", "medium", "slow", "archival", "forever",
        ];
        for preset in ["continuum", "titans", "moneta", "yaad", "memora"] {
            let c = cfg(&format!("memory:\n  miras:\n    preset: {preset}\n"));
            assert_eq!(c.memory.preset, preset);
            assert_eq!(c.memory.bands, eight);
        }
        let c = cfg("memory:\n  miras:\n    preset: flat\n    bands: [{name: x}]\n");
        assert_eq!(c.memory.bands, vec!["flat"]);
        let c = cfg(
            "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: a\n      - max_entries: 3\n",
        );
        assert_eq!(c.memory.preset, "custom");
        assert_eq!(c.memory.bands, vec!["a", "band"]);
        // A string `bands` is iterated by Python; harmless outside custom.
        assert_eq!(
            cfg("memory:\n  miras:\n    bands: abc\n").memory.bands,
            vec!["flat"]
        );
        assert!(refused("memory:\n  miras:\n    preset: Flat\n"));
        assert!(refused("memory:\n  miras:\n    preset:\n"));
        assert!(refused("memory:\n  miras:\n    preset: custom\n"));
        assert!(refused(
            "memory:\n  miras:\n    preset: custom\n    bands: []\n"
        ));
        // `5 or []` is iterated: TypeError.
        assert!(refused("memory:\n  miras:\n    bands: 5\n"));
    }

    #[test]
    fn search_validators() {
        let c = cfg("memory:\n  search:\n    fusion: rrf\n    min_score: 1\n");
        assert_eq!(c.memory.search.fusion, "rrf");
        assert_eq!(c.memory.search.min_score, 1.0);
        for text in [
            "memory:\n  search:\n    fusion: max\n",
            "memory:\n  search:\n    fusion:\n",
            "memory:\n  search:\n    min_score: true\n",
            "memory:\n  search:\n    min_score: 1.5\n",
            "memory:\n  search:\n    min_score: -0.1\n",
            "memory:\n  search:\n    min_score: .nan\n",
            "memory:\n  search:\n    min_score: '0.5'\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
    }

    #[test]
    fn memory_values_are_read() {
        let c = cfg(
            "memory:\n  top_k: 12\n  hide_superseded: true\n  search_confidence_floor: 0.3\n  \
                     recency_boost_enabled: true\n  bm25:\n    enabled: false\n    k1: 2\n    b: 0.5\n    \
                     weight: 0.4\n    top_n: 30\n    min_score: 0.2\n  reranker:\n    enabled: true\n  \
                     search:\n    candidate_pool_multiplier: 3\n    contiguity_neighbors: 2\n    \
                     timeline_channel: true\n",
        );
        assert_eq!(c.memory.top_k, 12);
        assert!(c.memory.hide_superseded);
        assert_eq!(c.memory.search_confidence_floor, 0.3);
        assert!(c.memory.recency_boost_enabled);
        assert_eq!(
            c.memory.bm25,
            Bm25Config {
                enabled: false,
                k1: 2.0,
                b: 0.5,
                weight: 0.4,
                top_n: 30,
                min_score: 0.2
            }
        );
        assert!(c.memory.reranker_enabled);
        assert_eq!(c.memory.search.candidate_pool_multiplier, 3);
        assert_eq!(c.memory.search.contiguity_neighbors, 2);
        assert!(c.memory.search.timeline_channel);
        let e = cfg(
            "embedding:\n  model_name: m\n  device: cpu\n  backend: onnx\n  query_prefix: ''\n  \
                     max_seq_length: 256\n",
        )
        .embedding;
        assert_eq!(
            e,
            EmbeddingConfig {
                model_name: "m".into(),
                device: "cpu".into(),
                backend: "onnx".into(),
                query_prefix: String::new(),
                max_seq_length: 256,
                ..EmbeddingConfig::default()
            }
        );
    }

    #[test]
    fn half_life_overlay() {
        let half_life = |text: &str| cfg(text).memory.recency_base_half_life_s;
        assert_eq!(half_life("memory:\n  recency_base_half_life_s: 10\n"), 10.0);
        assert_eq!(half_life("memory:\n  top_k: 3\n"), 86400.0);
        // A non-empty mapping is not a leaf: the overlay fills it.
        assert_eq!(
            half_life("memory:\n  recency_base_half_life_s:\n    a: 1\n"),
            86400.0
        );
        // A top-level dotted key is a leaf of that spelling, so the overlay
        // stays off and the library default stands.
        assert_eq!(half_life("memory.recency_base_half_life_s: 5\n"), 3600.0);
    }

    #[test]
    fn sections_python_refuses_when_not_mappings() {
        for text in [
            "embedding:\n",
            "embedding: x\n",
            "memory:\n  reference:\n",
            "memory:\n  meta_filter:\n",
            "memory:\n  traces:\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
        // The overlay skips a key the user set, even through a dotted key.
        assert!(!refused(
            "memory.meta_filter.enabled: 1\nmemory:\n  meta_filter:\n"
        ));
    }

    #[test]
    fn wake_and_coordination_validators() {
        let c =
            cfg("coordination:\n  wake:\n    nudge_interval_seconds: 5\n    nightly_total: 7\n");
        assert_eq!(c.coordination.wake.nightly_total, 7);
        let c = cfg("coordination:\n  bogus: 1\n  enabled: false\n");
        assert!(!c.coordination.enabled);
        let c = cfg("coordination:\n  wake:\n    active_seconds: 1\n");
        assert_eq!(c.coordination.wake.active_seconds, 1);
        assert!(!refused(
            "coordination:\n  maintainer_principals: [alice, ' Bob ']\n  allowed_principals: [x]\n  \
             awareness_limit: 20\n  audit_retention_days: 0\n  \
             maintainer: {rp_id: h, origin: 'https://h', maintainer_per_recipient_per_hour: 0}\n"
        ));
        for text in [
            "coordination:\n  wake:\n    nightly_total: -1\n",
            "coordination:\n  wake:\n    nightly_total: 5.0\n",
            "coordination:\n  wake:\n    nightly_total: true\n",
            "coordination:\n  wake:\n    active_seconds: 0\n",
            "coordination:\n  wake:\n    bogus: 5\n",
            "coordination:\n  wake:\n    1: 5\n",
            "coordination:\n  wake:\n",
            "coordination:\n  enabled: 'yes'\n",
            "coordination:\n  enabled: 1\n",
            "coordination:\n  awareness_limit: 21\n",
            "coordination:\n  awareness_limit: 0\n",
            "coordination:\n  audit_retention_days: -1\n",
            "coordination:\n  allowed_principals: ['  ']\n",
            "coordination:\n  allowed_principals: alice\n",
            "coordination:\n  allowed_principals:\n",
            "coordination:\n  daemon_notice_principals: [1]\n",
            "coordination:\n  maintainer_principals: [' Default ']\n",
            "coordination:\n  maintainer_principals: [DAEMON]\n",
            "coordination:\n  maintainer:\n",
            "coordination:\n  maintainer:\n    bogus: 1\n",
            "coordination:\n  maintainer:\n    rp_id: 5\n",
            "coordination:\n  maintainer:\n    maintainer_per_recipient_per_hour: -1\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
    }

    #[test]
    fn wake_to_json_keeps_field_order() {
        let json = serde_json::to_string(&WakeConfig::default().to_json()).unwrap();
        assert_eq!(
            json,
            r#"{"per_recipient_per_hour":20,"urgent_per_sender_per_hour":6,"nightly_total":200,"fan_out_stagger_seconds":30,"active_seconds":60,"authority_per_sender_per_hour":12}"#
        );
    }

    #[test]
    fn updates_policy_and_dream_validators() {
        let c = cfg(
            "updates:\n  check_releases: false\n  check_interval_seconds: 60\n  unattended_daemon: true\n",
        );
        assert_eq!(
            c.updates,
            UpdatesConfig {
                check_releases: false,
                unattended_clients: false,
                unattended_daemon: true,
                check_interval_seconds: 60,
            }
        );
        assert!(!refused(
            "memory_policy:\n  variant: none\n  ab_arms: [none, none]\n"
        ));
        assert!(!refused("memory_policy: 5\n"));
        assert!(!refused("memory:\n  dream:\n    stall_repeat_hours: 0.5\n"));
        for text in [
            "updates:\n  check_releases: 1\n",
            "updates:\n  unattended_clients:\n",
            "updates:\n  check_interval_seconds: 59\n",
            "updates:\n  check_interval_seconds: 60.0\n",
            "memory_policy:\n  variant: bogus\n",
            "memory_policy:\n  ab_arms: [none]\n",
            "memory_policy:\n  ab_arms: [none, bogus]\n",
            "memory_policy:\n  ab_arms:\n",
            "memory:\n  dream:\n    stall_repeat_hours: 0\n",
            "memory:\n  dream:\n    stall_repeat_hours: true\n",
            "memory:\n  dream:\n    stall_repeat_hours: .inf\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
    }

    /// Declared divergences: Python starts with these (a `None` section or
    /// an untyped value) and fails later; this port refuses at load.
    #[test]
    fn wrong_types_python_accepts_refuse_here() {
        for text in [
            "memory:\n  search:\n",
            "memory:\n  bm25: 1\n",
            "memory:\n  dream:\n",
            "coordination:\n",
            "updates:\n",
            "memory:\n  recency_base_half_life_s:\n",
            "memory:\n  recency_base_half_life_s: {}\n",
            "memory:\n  top_k: 8.0\n",
            "memory:\n  hide_superseded: 1\n",
            "memory:\n  bm25:\n    k1: x\n",
            "embedding:\n  query_prefix:\n",
            "memory:\n  miras:\n    preset: custom\n    bands: [x, y]\n",
            "memory:\n  miras:\n    preset: custom\n    bands: [{name: 5}]\n",
            "memory:\n  dream:\n    extractor_base_url: 5\n",
        ] {
            assert!(refused(text), "{text:?}");
        }
    }

    /// yaml-rust2 resolves plain scalars by YAML 1.2 core rules; PyYAML by
    /// YAML 1.1. What each form gives here, with PyYAML's reading beside it.
    #[test]
    fn yaml_1_1_forms_documented() {
        // Python: True. Here: the string "yes" -> refused as a non-bool.
        assert!(refused("memory:\n  hide_superseded: yes\n"));
        assert!(refused("coordination:\n  enabled: on\n"));
        // Python: 8 (octal). Here: 10.
        assert_eq!(cfg("memory:\n  top_k: 010\n").memory.top_k, 10);
        // Python: 10. Here: the string "1_0" -> refused.
        assert!(refused("memory:\n  top_k: 1_0\n"));
        // Both: 16.
        assert_eq!(cfg("memory:\n  top_k: 0x10\n").memory.top_k, 16);
        // Python: the string "0o10". Here: 8.
        assert_eq!(cfg("memory:\n  top_k: 0o10\n").memory.top_k, 8);
        // Python: the string "1e-1" (refused by the min_score validator).
        // Here: 0.1.
        assert_eq!(
            cfg("memory:\n  search:\n    min_score: 1e-1\n")
                .memory
                .search
                .min_score,
            0.1
        );
        // Both: 0.1.
        assert_eq!(
            cfg("memory:\n  search:\n    min_score: 1.0e-1\n")
                .memory
                .search
                .min_score,
            0.1
        );
        // Python: 90 (sexagesimal) and 3 (binary). Here: strings -> refused.
        assert!(refused("memory:\n  top_k: 1:30\n"));
        assert!(refused("memory:\n  top_k: 0b11\n"));
        // Python: None. Here: the string "Null"; a flat preset ignores it
        // either way.
        assert_eq!(
            cfg("memory:\n  miras:\n    bands: Null\n").memory.bands,
            vec!["flat"]
        );
        // Python: a date at the top level (TypeError, refused). Here: a
        // string naming no section -> defaults.
        assert_eq!(cfg("2024-01-01\n"), Config::default());
        // Python: an int past i64 is still an int (accepted). Here: a float
        // -> refused.
        assert!(refused(
            "coordination:\n  wake:\n    nightly_total: 99999999999999999999\n"
        ));
    }

    #[test]
    fn extractor_resolution() {
        let none = env_of(&[]);
        let dream = |text: &str| cfg(text).dream;
        assert!(
            !dream("memory:\n  dream:\n    extractor_base_url: http://x\n")
                .extractor_configured(&none)
        );
        assert!(
            dream("memory:\n  dream:\n    extractor_base_url: http://x\n    extractor_model: m\n")
                .extractor_configured(&none)
        );
        assert!(
            dream("memory:\n  dream:\n    fallback_base_url: http://x\n    fallback_model: m\n")
                .extractor_configured(&none)
        );
        assert!(
            dream("memory:\n  dream:\n    extractor_base_url: http://x\n    extractor_model_override: o\n")
                .extractor_configured(&none)
        );
        assert!(
            !dream(
                "memory:\n  dream:\n    extractor_base_url: http://x\n    extractor_model: ''\n"
            )
            .extractor_configured(&none)
        );
        assert!(!dream("memory:\n  dream:\n    enabled: false\n").enabled);

        // Env mode (default): a non-empty variable wins, an empty one falls
        // back to the config value.
        let env_both = env_of(&[
            ("PSEUDOLIFE_DREAM_BASE_URL", "http://e"),
            ("PSEUDOLIFE_DREAM_MODEL", "m"),
        ]);
        assert!(DreamConfig::default().extractor_configured(&env_both));
        let env_fb = env_of(&[
            ("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL", "http://e"),
            ("PSEUDOLIFE_DREAM_FALLBACK_MODEL", "m"),
        ]);
        assert!(DreamConfig::default().extractor_configured(&env_fb));
        let env_url = env_of(&[
            ("PSEUDOLIFE_DREAM_BASE_URL", "http://e"),
            ("PSEUDOLIFE_DREAM_MODEL", ""),
        ]);
        assert!(!DreamConfig::default().extractor_configured(&env_url));
        assert!(
            dream("memory:\n  dream:\n    extractor_model: m\n").extractor_configured(&env_url)
        );

        // Config mode ignores the environment.
        let config_mode = dream("memory:\n  dream:\n    extractor_source: config\n");
        assert!(!config_mode.extractor_configured(&env_both));
        let config_set = dream(
            "memory:\n  dream:\n    extractor_source: config\n    extractor_base_url: http://x\n    \
             extractor_model: m\n",
        );
        assert!(config_set.extractor_configured(&none));
        // Any other source value is env mode.
        let other = dream("memory:\n  dream:\n    extractor_source: bogus\n");
        assert!(other.extractor_configured(&env_both));
    }

    #[test]
    fn config_paths() {
        let cwd = Path::new("/srv");
        let (data, file) = config_path(&env_of(&[]), cwd);
        assert_eq!(data, cwd.join("data"));
        assert_eq!(file, cwd.join("data").join("config.yaml"));
        let (data, file) = config_path(&env_of(&[("PSEUDOLIFE_MCP_DATA_DIR", "")]), cwd);
        assert_eq!(data, cwd.join("data"));
        assert_eq!(file, cwd.join("data").join("config.yaml"));
        let (data, file) = config_path(
            &env_of(&[
                ("PSEUDOLIFE_MCP_DATA_DIR", "/d"),
                ("PSEUDOLIFE_MCP_CONFIG", "/c.yaml"),
            ]),
            cwd,
        );
        assert_eq!(data, PathBuf::from("/d"));
        assert_eq!(file, PathBuf::from("/c.yaml"));
        let (_, file) = config_path(&env_of(&[("PSEUDOLIFE_MCP_CONFIG", "")]), cwd);
        assert_eq!(file, PathBuf::from("."));
    }

    #[test]
    fn daemon_env_defaults() {
        let e = DaemonEnv::from_env(&env_of(&[])).unwrap();
        assert_eq!(
            e,
            DaemonEnv {
                host: "127.0.0.1".into(),
                port: 8765,
                token: None,
                tokens_raw: None,
                trust_bind: false,
                database_url: None,
                release_check_disabled: false,
                plugin_dir: None,
                build: None,
            }
        );
    }

    #[test]
    fn daemon_env_parsing() {
        let e = DaemonEnv::from_env(&env_of(&[
            ("PSEUDOLIFE_MCP_HOST", ""),
            ("PSEUDOLIFE_MCP_PORT", " 8_080 "),
            ("PSEUDOLIFE_MCP_TOKEN", ""),
            ("PSEUDOLIFE_MCP_TOKENS", " "),
            ("PSEUDOLIFE_MCP_TRUST_BIND", "TRUE"),
            ("PSEUDOLIFE_MCP_DATABASE_URL", ""),
            ("PSEUDOLIFE_RELEASE_CHECK", " 0\n"),
            ("PSEUDOLIFE_PLUGIN_DIR", "/p"),
            ("PSEUDOLIFE_BUILD_GIT_SHA", "abc"),
            ("PSEUDOLIFE_BUILD_DIRTY", " True "),
            ("PSEUDOLIFE_BUILD_TIME", ""),
            ("PSEUDOLIFE_BUILD_SOURCE", ""),
        ]))
        .unwrap();
        assert_eq!(e.host, "");
        assert_eq!(e.port, 8080);
        assert_eq!(e.token, None);
        assert_eq!(e.tokens_raw.as_deref(), Some(" "));
        assert!(e.trust_bind);
        assert_eq!(e.database_url, None);
        assert!(e.release_check_disabled);
        assert_eq!(e.plugin_dir, Some(PathBuf::from("/p")));
        assert_eq!(
            e.build,
            Some(BuildStamp {
                git_sha: "abc".into(),
                dirty: Some(true),
                built_at: String::new(),
                source: "unknown".into(),
            })
        );

        let e = DaemonEnv::from_env(&env_of(&[
            ("PSEUDOLIFE_MCP_PORT", "+0"),
            ("PSEUDOLIFE_MCP_TOKEN", "t"),
            ("PSEUDOLIFE_MCP_TRUST_BIND", " 1"),
            ("PSEUDOLIFE_RELEASE_CHECK", "00"),
            ("PSEUDOLIFE_BUILD_GIT_SHA", "abc"),
            ("PSEUDOLIFE_BUILD_DIRTY", "yes"),
        ]))
        .unwrap();
        assert_eq!(e.port, 0);
        assert_eq!(e.token.as_deref(), Some("t"));
        assert!(!e.trust_bind);
        assert!(!e.release_check_disabled);
        let build = e.build.unwrap();
        assert_eq!(build.dirty, None);
        assert_eq!(build.built_at, "unknown");
        assert_eq!(build.source, "unknown");

        for port in ["", "abc", "8_", "8__0", "0x10", "1.0"] {
            assert!(
                DaemonEnv::from_env(&env_of(&[("PSEUDOLIFE_MCP_PORT", port)])).is_err(),
                "{port:?}"
            );
        }
        // `int()` accepts these; Python fails only at bind, after the guards.
        for (port, want) in [("-1", -1), ("70000", 70000), ("65536", 65536)] {
            let e = DaemonEnv::from_env(&env_of(&[("PSEUDOLIFE_MCP_PORT", port)])).unwrap();
            assert_eq!(e.port, want, "{port:?}");
        }
        let empty_sha = env_of(&[("PSEUDOLIFE_BUILD_GIT_SHA", "")]);
        assert!(DaemonEnv::from_env(&empty_sha).unwrap().build.is_none());
    }
}
