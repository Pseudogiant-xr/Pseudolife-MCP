//! Placeholder interface (replaced by the cortex port).
pub struct CortexStore;
pub async fn hydrate(_client: &tokio_postgres::Client) -> anyhow::Result<CortexStore> {
    Ok(CortexStore)
}
