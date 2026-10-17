//! `freshness.resolve_class` (`memory/freshness.py:98-127`): the freshness
//! class `memory_fact_set` gives a fact when the caller passes `"auto"`, from
//! the subject's entity kind and the normalised attribute.

use regex::Regex;
use std::sync::LazyLock;

/// `_EVENT_ATTRIBUTE_RE`: events and measurements never rot.
static EVENT_ATTRIBUTE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(^|[-_])(date|at|hash|commit|count|score|id|size|duration)$").expect("regex")
});

/// `_VOLATILE_ATTRIBUTE_RE`: state attributes of a `system` entity.
static VOLATILE_ATTRIBUTE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(^|[-_])(status|state|health|live|running|current|deployment|deployed|url)([-_]|$)|(^|[-_])version([-_]|$)",
    )
    .expect("regex")
});

/// `resolve_class(kind, attribute_norm)`: `"volatile"` only for a `system`
/// entity whose attribute names state and not an event; every other case,
/// unknown kinds included, is `"evergreen"`.
///
/// Python's `$` also matches before one final newline; `attribute_norm` is a
/// slot key (`norm_key`), which never ends in one, so `$` here is the same.
pub fn resolve_class(kind: Option<&str>, attribute_norm: &str) -> &'static str {
    let kind = crate::stores::cortex::py_casefold(
        kind.unwrap_or("")
            .trim_matches(crate::stores::cortex::py_isspace),
    );
    if kind != "system" {
        return "evergreen";
    }
    let attr = crate::stores::cortex::py_casefold(
        attribute_norm.trim_matches(crate::stores::cortex::py_isspace),
    );
    if EVENT_ATTRIBUTE.is_match(&attr) {
        return "evergreen";
    }
    if VOLATILE_ATTRIBUTE.is_match(&attr) {
        "volatile"
    } else {
        "evergreen"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_class_matches_python() {
        // Values from freshness.resolve_class on CPython 3.11.
        for (kind, attr, want) in [
            (Some("system"), "status", "volatile"),
            (Some("System "), "deployment-status", "volatile"),
            (Some("system"), "schema-version", "volatile"),
            (Some("system"), "version", "volatile"),
            (Some("system"), "url", "volatile"),
            (Some("system"), "deployment-date", "evergreen"),
            (Some("system"), "commit", "evergreen"),
            (Some("system"), "last-run-at", "evergreen"),
            (Some("system"), "owner", "evergreen"),
            (Some("system"), "statusline", "evergreen"),
            (Some("system"), "versioning", "evergreen"),
            (Some("system"), "current_state", "volatile"),
            (Some("artifact"), "status", "evergreen"),
            (Some("concept"), "version", "evergreen"),
            (None, "status", "evergreen"),
            (Some(""), "status", "evergreen"),
        ] {
            assert_eq!(resolve_class(kind, attr), want, "{kind:?} {attr}");
        }
    }
}
