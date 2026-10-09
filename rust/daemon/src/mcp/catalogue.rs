//! The MCP tool catalogue: every tool object exactly as the Python daemon
//! serializes it in `tools/list`, recorded by `harness/mcp_catalogue.py`.
//!
//! The objects are embedded as raw JSON text, one per line, so a tier's list
//! is a byte-for-byte concatenation of the recorded objects (spec M3). The
//! tier of each tool is the lowest tier whose list showed it (`tiers.json`).

use super::tiers::Tier;
use std::collections::BTreeSet;
use std::sync::OnceLock;

const TOOLS_JSONL: &str = include_str!("tools.jsonl");
const TIERS_JSON: &str = include_str!("tiers.json");

pub struct Tool {
    pub name: String,
    pub tier: Tier,
    /// The tool object's exact JSON text.
    pub raw: &'static str,
}

pub fn tools() -> &'static [Tool] {
    static TOOLS: OnceLock<Vec<Tool>> = OnceLock::new();
    TOOLS.get_or_init(|| {
        let tiers: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(TIERS_JSON).expect("tiers.json is a JSON object");
        TOOLS_JSONL
            .lines()
            .filter(|l| !l.is_empty())
            .map(|raw| {
                let v: serde_json::Value = serde_json::from_str(raw).expect("tools.jsonl line");
                let name = v["name"].as_str().expect("tool name").to_string();
                let tier = tiers
                    .get(&name)
                    .and_then(|t| t.as_str())
                    .and_then(Tier::parse)
                    .expect("every tool has a tier");
                Tool { name, tier, raw }
            })
            .collect()
    })
}

pub fn find(name: &str) -> Option<&'static Tool> {
    tools().iter().find(|t| t.name == name)
}

/// Names visible at `tier` (`_visible_tool_names`).
pub fn visible(tier: Tier) -> BTreeSet<&'static str> {
    tools()
        .iter()
        .filter(|t| t.tier <= tier)
        .map(|t| t.name.as_str())
        .collect()
}

/// The `result` object of `tools/list` at `tier`, as JSON text: registration
/// order, no `nextCursor`.
pub fn list_result(tier: Tier) -> String {
    let mut out = String::from("{\"tools\":[");
    let mut first = true;
    for t in tools().iter().filter(|t| t.tier <= tier) {
        if !first {
            out.push(',');
        }
        first = false;
        out.push_str(t.raw);
    }
    out.push_str("]}");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalogue_is_cumulative_and_complete() {
        let n = |t| visible(t).len();
        assert_eq!(tools().len(), 38);
        assert_eq!(
            (n(Tier::Minimal), n(Tier::Core), n(Tier::Full)),
            (10, 24, 38)
        );
        assert!(visible(Tier::Minimal).contains("memory_toolset"));
    }

    #[test]
    fn each_line_reserializes_to_itself() {
        // serde_json here preserves key order and number text, so a line that
        // parses and re-serializes to different bytes was hand-edited.
        for t in tools() {
            let v: serde_json::Value = serde_json::from_str(t.raw).unwrap();
            assert_eq!(serde_json::to_string(&v).unwrap(), t.raw, "{}", t.name);
        }
    }
}
