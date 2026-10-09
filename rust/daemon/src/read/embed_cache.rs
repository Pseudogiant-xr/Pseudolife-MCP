//! The encode LRU (spec S15, `memory/embedding.py:406-484`): one
//! process-wide cache of normalised vectors keyed by the text as encoded.
//! Query keys carry the query prefix, so they never collide with document
//! encodes of the same raw text. Observable only as latency.

use crate::embed::Embedder;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

struct Lru {
    map: HashMap<String, (Vec<f32>, u64)>,
    tick: u64,
}

fn cache() -> &'static Mutex<Lru> {
    static CACHE: OnceLock<Mutex<Lru>> = OnceLock::new();
    CACHE.get_or_init(|| {
        Mutex::new(Lru {
            map: HashMap::new(),
            tick: 0,
        })
    })
}

/// `encode_query`: the query embedded through the cache. `cap` is
/// `embedding.cache_size`; 0 disables the cache.
pub fn encode_query(embedder: &Embedder, query: &str, cap: i64) -> anyhow::Result<Vec<f32>> {
    let key = format!("q\u{0}{query}");
    if cap > 0 {
        let mut c = cache().lock().expect("embed cache");
        c.tick += 1;
        let tick = c.tick;
        if let Some((v, t)) = c.map.get_mut(&key) {
            *t = tick;
            return Ok(v.clone());
        }
    }
    let v = embedder.embed_query(query)?;
    if cap > 0 {
        let mut c = cache().lock().expect("embed cache");
        c.tick += 1;
        let tick = c.tick;
        c.map.insert(key, (v.clone(), tick));
        while c.map.len() > cap as usize {
            let oldest = c
                .map
                .iter()
                .min_by_key(|(_, (_, t))| *t)
                .map(|(k, _)| k.clone());
            match oldest {
                Some(k) => c.map.remove(&k),
                None => break,
            };
        }
    }
    Ok(v)
}
