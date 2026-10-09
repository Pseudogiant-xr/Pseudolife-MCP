//! The Console route table (spec R8), embedded from the golden the harness
//! records from Python's `ConsoleRoutes` (`harness/record_routes.py`).

use std::collections::HashSet;
use std::sync::OnceLock;

const GOLDEN: &str = include_str!("../harness/goldens/routes.json");

pub struct RouteTable {
    get: HashSet<String>,
    post: HashSet<String>,
}

impl RouteTable {
    fn parse(text: &str) -> RouteTable {
        let v: serde_json::Value = serde_json::from_str(text).expect("routes.json is valid JSON");
        let set = |k: &str| -> HashSet<String> {
            v[k].as_array()
                .expect("routes.json lists GET and POST")
                .iter()
                .map(|p| p.as_str().expect("a route is a string").to_string())
                .collect()
        };
        RouteTable {
            get: set("GET"),
            post: set("POST"),
        }
    }

    pub fn get() -> &'static RouteTable {
        static TABLE: OnceLock<RouteTable> = OnceLock::new();
        TABLE.get_or_init(|| RouteTable::parse(GOLDEN))
    }

    /// `ConsoleRoutes.table.get((method, path))` is not None.
    pub fn handles(&self, method: &str, path: &str) -> bool {
        match method {
            "GET" => self.get.contains(path),
            "POST" => self.post.contains(path),
            _ => false,
        }
    }

    /// `ConsoleRoutes.has(path)`: the path is registered under any method.
    pub fn has(&self, path: &str) -> bool {
        self.get.contains(path) || self.post.contains(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn golden_table_matches_python_counts_and_verbs() {
        let t = RouteTable::get();
        assert_eq!(t.get.len() + t.post.len(), 72);
        assert!(t.handles("GET", "/api/search"));
        assert!(!t.handles("POST", "/api/search"));
        assert!(t.handles("GET", "/api/config") && t.handles("POST", "/api/config"));
        assert!(t.has("/api/delete") && !t.handles("GET", "/api/delete"));
        assert!(!t.has("/api/nope"));
    }
}
