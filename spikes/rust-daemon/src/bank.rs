//! Read-only hydration of a schema-55 bank into a resident matrix (spec S7).

use anyhow::{bail, Context, Result};
use serde_json::Value;
use tokio_postgres::Client;

pub const SCHEMA: i64 = 55;
pub const DIM: usize = 1024;

pub struct Entry {
    pub id: i64,
    pub band: String,
    pub text: String,
    pub surprise: f32,
    pub ts: f64,
    pub access_count: i32,
    pub source: String,
    pub superseded_at: Option<f64>,
    pub superseded_by_text: Option<String>,
    pub episode_id: Option<String>,
    pub episode_title: Option<String>,
    pub tags: Vec<String>,
    /// `[entity, attribute, value, polarity]` rows as stored.
    pub slots: Vec<Value>,
    pub authority: Option<String>,
    pub distortion_tolerance: Option<String>,
}

pub struct Bank {
    pub entries: Vec<Entry>,
    /// Row-major, L2-normalized, `entries.len() * DIM`.
    pub matrix: Vec<f32>,
}

/// Every session is read-only: no DDL and no writes can leave the spike.
pub async fn read_only(client: &Client) -> Result<()> {
    client.batch_execute("SET default_transaction_read_only = on").await?;
    Ok(())
}

pub async fn check_schema(client: &Client) -> Result<()> {
    let row = client
        .query_opt("SELECT value::text FROM meta WHERE key = 'schema_version'", &[])
        .await
        .context("reading meta.schema_version")?;
    // meta.value is JSON: a number, or a string holding one.
    let v: Option<String> = row.map(|r| r.get(0));
    let v = v.map(|s| s.trim().trim_matches('"').to_string());
    match v.as_deref().map(str::parse::<i64>) {
        Some(Ok(SCHEMA)) => Ok(()),
        other => bail!("bank schema is {other:?}, the spike reads schema {SCHEMA} only"),
    }
}

fn normalize(v: &mut [f32]) {
    // torch F.normalize(p=2, eps=1e-12)
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt().max(1e-12);
    v.iter_mut().for_each(|x| *x /= n);
}

pub async fn hydrate(client: &Client) -> Result<Bank> {
    let rows = client
        .query(
            "SELECT id, band, text, embedding::real[], surprise, ts, access_count, source, \
             superseded_at, superseded_by_text, episode_id, episode_title, tags, slots, \
             authority, distortion_tolerance FROM entries ORDER BY id",
            &[],
        )
        .await?;
    let mut entries = Vec::with_capacity(rows.len());
    let mut matrix = Vec::with_capacity(rows.len() * DIM);
    for r in rows {
        let mut emb: Vec<f32> = r.get(3);
        if emb.len() != DIM {
            bail!("entry {} has a {}-dim vector, expected {DIM}", r.get::<_, i64>(0), emb.len());
        }
        normalize(&mut emb);
        matrix.extend_from_slice(&emb);
        let tags: Option<Value> = r.get(12);
        let slots: Option<Value> = r.get(13);
        entries.push(Entry {
            id: r.get(0),
            band: r.get(1),
            text: r.get(2),
            surprise: r.get::<_, Option<f32>>(4).unwrap_or(0.0),
            ts: r.get::<_, Option<f64>>(5).unwrap_or(0.0),
            access_count: r.get::<_, Option<i32>>(6).unwrap_or(0),
            source: r.get::<_, Option<String>>(7).unwrap_or_default(),
            superseded_at: r.get(8),
            superseded_by_text: r.get(9),
            episode_id: r.get(10),
            episode_title: r.get(11),
            tags: match tags {
                Some(Value::Array(a)) => a.into_iter().filter_map(|t| t.as_str().map(String::from)).collect(),
                _ => Vec::new(),
            },
            slots: match slots {
                Some(Value::Array(a)) => a,
                _ => Vec::new(),
            },
            authority: r.get(14),
            distortion_tolerance: r.get(15),
        });
    }
    Ok(Bank { entries, matrix })
}

pub fn normalize_query(v: &mut [f32]) {
    normalize(v)
}
