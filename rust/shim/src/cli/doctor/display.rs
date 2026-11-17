//! Credential-safe display of diagnostic text and paths.
use serde_json::{Value, json};

/// Response fields and local path labels are untrusted text too. Redact before
/// serialization so quoting cannot disguise a credential in an error field.
pub(super) fn redact_report(value: &mut Value, token: Option<&str>) {
    match value {
        Value::String(text) => {
            if let Some(token) = token.filter(|token| !token.is_empty()) {
                *text = text.replace(token, "[redacted]");
            }
            static URLS: std::sync::LazyLock<regex_lite::Regex> = std::sync::LazyLock::new(|| {
                regex_lite::Regex::new(r#"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"<>]+"#)
                    .expect("URL pattern")
            });
            *text = URLS
                .replace_all(text, |matched: &regex_lite::Captures<'_>| {
                    let url = matched.get(0).unwrap().as_str();
                    let authority = url
                        .split_once("://")
                        .unwrap()
                        .1
                        .split(['/', '?', '#'])
                        .next()
                        .unwrap();
                    if authority.contains('@') || url.contains(['?', '#']) {
                        "[redacted URL]".to_owned()
                    } else {
                        url.to_owned()
                    }
                })
                .into_owned();
        }
        Value::Array(items) => items
            .iter_mut()
            .for_each(|value| redact_report(value, token)),
        Value::Object(items) => items
            .values_mut()
            .for_each(|value| redact_report(value, token)),
        _ => {}
    }
}

pub(super) fn display_text(text: &str, token: Option<&str>) -> String {
    let mut value = json!(text);
    redact_report(&mut value, token);
    value.as_str().unwrap().to_owned()
}

pub(super) fn display_path(path: &str, token: Option<&str>) -> String {
    let shown = display_text(path, token);
    if shown == path {
        shown
    } else {
        "[redacted]".into()
    }
}
