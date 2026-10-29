//! `derive_session_title` (`session_title.py:32-59`): the content title a
//! generic-named session root gets when it closes with entries.

/// `_NOISE_SOURCES`: sources that win only when they are all there is.
const NOISE_SOURCES: [&str; 5] = ["status", "log", "claude", "agent", "conversation"];

/// `_SNIPPET_CHARS`.
const SNIPPET_CHARS: usize = 60;

/// Python's `str.isspace`, as `str.split()` and `str.strip()` use it.
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `"{dominant_source} - {start stamp}: {first-entry snippet}"`, from the
/// subtree's `(timestamp, source, text)` entries; None without entries.
pub fn derive_session_title(started_at: f64, entries: &[(f64, &str, &str)]) -> Option<String> {
    if entries.is_empty() {
        return None;
    }
    // `counts` in first-seen order; `max` keeps the first of equal counts.
    let mut counts: Vec<(String, usize)> = Vec::new();
    for (_, source, _) in entries {
        let stripped = source.trim_matches(py_space);
        let s = if stripped.is_empty() {
            "session"
        } else {
            stripped
        };
        match counts.iter_mut().find(|(k, _)| k == s) {
            Some((_, n)) => *n += 1,
            None => counts.push((s.to_string(), 1)),
        }
    }
    let signal: Vec<&(String, usize)> = counts
        .iter()
        .filter(|(s, _)| !NOISE_SOURCES.contains(&s.as_str()))
        .collect();
    let pool: Vec<&(String, usize)> = if signal.is_empty() {
        counts.iter().collect()
    } else {
        signal
    };
    let mut src = pool[0];
    for candidate in &pool[1..] {
        if candidate.1 > src.1 {
            src = candidate;
        }
    }
    // `min(entries, key=timestamp)`: the first of equal minima.
    let mut first = &entries[0];
    for e in &entries[1..] {
        if e.0 < first.0 {
            first = e;
        }
    }
    let words: Vec<&str> = first.2.split(py_space).filter(|w| !w.is_empty()).collect();
    let mut snippet = words.join(" ");
    let chars: Vec<char> = snippet.chars().collect();
    if chars.len() > SNIPPET_CHARS {
        // `snippet.rfind(" ", 0, 60)`: the last space wholly inside [0, 60).
        let cut = chars[..SNIPPET_CHARS].iter().rposition(|&c| c == ' ');
        let end = match cut {
            Some(i) if i > 20 => i,
            _ => SNIPPET_CHARS,
        };
        let head: String = chars[..end].iter().collect();
        snippet = format!("{}…", head.trim_end_matches(py_space));
    }
    let stamp = local_stamp(started_at);
    Some(if snippet.is_empty() {
        format!("{} - {stamp}", src.0)
    } else {
        format!("{} - {stamp}: {snippet}", src.0)
    })
}

/// `time.strftime("%Y-%m-%d %H:%M", time.localtime(t))`.
fn local_stamp(t: f64) -> String {
    use chrono::TimeZone;
    let secs = t.floor() as i64;
    match chrono::Local.timestamp_opt(secs, 0) {
        chrono::LocalResult::Single(dt) | chrono::LocalResult::Ambiguous(dt, _) => {
            dt.format("%Y-%m-%d %H:%M").to_string()
        }
        chrono::LocalResult::None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dominant_signal_source_and_first_entry_snippet() {
        let t = derive_session_title(
            0.0,
            &[
                (5.0, "agent", "later"),
                (2.0, " proj ", "  first   entry\u{1f}text "),
                (3.0, "proj", "x"),
                (4.0, "other", "y"),
            ],
        )
        .unwrap();
        assert!(t.starts_with("proj - "), "{t}");
        assert!(t.ends_with(": first entry text"), "{t}");
        // Only noise: the most frequent noise source.
        let t = derive_session_title(0.0, &[(1.0, "status", "a"), (2.0, "", "b"), (3.0, "", "c")]);
        assert!(t.unwrap().starts_with("session - "));
        assert_eq!(derive_session_title(0.0, &[]), None);
    }

    #[test]
    fn long_snippets_cut_at_a_word_after_twenty_chars() {
        let long = "word ".repeat(20);
        let t = derive_session_title(0.0, &[(1.0, "p", long.as_str())]).unwrap();
        let snippet = t.split_once(": ").unwrap().1;
        // rfind(" ", 0, 60) is index 59; the cut keeps 59 chars, rstripped.
        assert_eq!(snippet, format!("{}…", "word ".repeat(12).trim_end()));
        let one = "x".repeat(70);
        let t = derive_session_title(0.0, &[(1.0, "p", one.as_str())]).unwrap();
        assert_eq!(
            t.split_once(": ").unwrap().1,
            format!("{}…", "x".repeat(60))
        );
        let t = derive_session_title(0.0, &[(1.0, "p", "  ")]).unwrap();
        assert!(!t.contains(": "));
    }
}
