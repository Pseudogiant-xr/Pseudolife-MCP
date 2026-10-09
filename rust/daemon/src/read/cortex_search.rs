//! Placeholder interface (replaced by the cortex port).
pub struct CortexKnobs;
impl CortexKnobs {
    pub fn from_config(_c: &crate::config::Config) -> CortexKnobs {
        CortexKnobs
    }
}
#[allow(clippy::too_many_arguments)]
pub async fn cortex_search(
    _store: &crate::stores::cortex::CortexStore,
    _db: &tokio_postgres::Client,
    _qvec: &[f32],
    _query: &str,
    _top_k: usize,
    _min_score: f64,
    _knobs: &CortexKnobs,
    _now: f64,
) -> anyhow::Result<Vec<serde_json::Value>> {
    Ok(Vec::new())
}
pub async fn attach_served_facts(
    _db: &tokio_postgres::Client,
    _event_id: i64,
    _facts: &[serde_json::Value],
) -> anyhow::Result<()> {
    Ok(())
}
