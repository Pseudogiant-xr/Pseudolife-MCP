//! Placeholder interface (replaced by the rerank port).
pub struct Reranker;
impl Reranker {
    pub fn load(_dir: &std::path::Path, _threads: usize) -> anyhow::Result<Reranker> {
        anyhow::bail!("not built")
    }
    pub fn rerank(&self, _q: &str, _c: &[&str]) -> anyhow::Result<Vec<f64>> {
        Ok(Vec::new())
    }
}
