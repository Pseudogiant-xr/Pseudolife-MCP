//! `update_cli._dump_complete`: a plain-format gzip dump ends with
//! pg_dump's closing marker, read outside COPY data.
//!
//! Python reads the dump as UTF-8 text with `errors="replace"` and
//! universal newlines (`\r\n` and a lone `\r` end a line as `\n` does), so
//! lines are split on those bytes here. Every comparison is with ASCII text,
//! which a replacement character never equals, so raw bytes compare the
//! same; only the COPY pattern's `\S` reads the line as characters.
use std::{collections::BTreeMap, path::Path};

use super::{
    Verdict,
    gzip::{self, Stop},
};

const MARKER: &[u8] = b"-- PostgreSQL database dump complete";

#[derive(Default)]
struct Lines {
    line: Vec<u8>,
    carriage_return: bool,
    in_copy: bool,
    complete: bool,
    /// The table of the COPY section being read, and each table's rows.
    table: String,
    counts: BTreeMap<String, i64>,
}

/// CPython's `str.isspace`, which `\s` in a str pattern follows.
pub(super) fn python_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// `re.match(r"^COPY \S+ .*FROM stdin;$", line)`: a run of non-space
/// characters must end at a plain space, and the rest end the line with
/// `FROM stdin;` (a line holds no `\n`, which `.` and `$` would treat apart).
fn copy_start(line: &[u8]) -> Option<String> {
    let rest = line.strip_prefix(b"COPY ")?;
    let rest = String::from_utf8_lossy(rest);
    let token = rest
        .char_indices()
        .find(|(_, character)| python_space(*character))
        .map_or(rest.len(), |(at, _)| at);
    (token > 0
        && rest[token..]
            .strip_prefix(' ')
            .is_some_and(|tail| tail.ends_with("FROM stdin;")))
    .then(|| rest[..token].to_owned())
}

impl Lines {
    fn end_line(&mut self) {
        let line = std::mem::take(&mut self.line);
        if self.in_copy {
            if line == b"\\." {
                self.in_copy = false;
            } else {
                *self.counts.entry(self.table.clone()).or_default() += 1;
            }
        } else if let Some(table) = copy_start(&line) {
            self.in_copy = true;
            self.counts.entry(table.clone()).or_default();
            self.table = table;
        } else if line == MARKER {
            self.complete = true;
        }
    }

    fn feed(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            if std::mem::take(&mut self.carriage_return) && byte == b'\n' {
                continue;
            }
            match byte {
                b'\r' => {
                    self.end_line();
                    self.carriage_return = true;
                }
                b'\n' => self.end_line(),
                _ => self.line.push(byte),
            }
        }
    }
}

pub(super) fn complete(path: &Path) -> Verdict {
    complete_with_counts(path).0
}

/// [`complete`], and the rows of each table's COPY section (`schema.table`
/// as pg_dump names it; Python move's `scan_dump` counts), for checking a
/// restored copy against the dump.
pub(super) fn complete_with_counts(path: &Path) -> (Verdict, BTreeMap<String, i64>) {
    let mut lines = Lines::default();
    let read = gzip::stream(path, |chunk| {
        lines.feed(chunk);
        Ok::<(), Stop>(())
    });
    if let Err(stop) = read {
        return (stop.into(), BTreeMap::new());
    }
    if !lines.line.is_empty() {
        lines.end_line();
    }
    let verdict = if lines.complete {
        Verdict::Pass
    } else {
        Verdict::Refuse
    };
    (verdict, lines.counts)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::restore_cli::tests::Scratch;
    use flate2::{Compression, write::GzEncoder};
    use std::io::Write;

    fn verdict(text: &[u8]) -> Verdict {
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(text).unwrap();
        let file = Scratch::new(&encoder.finish().unwrap());
        complete(&file.0)
    }

    #[test]
    fn each_copy_section_counts_its_rows() {
        let text = concat!(
            "SET x;\n",
            "COPY public.meta (key, value) FROM stdin;\na\t1\nb\t2\n\\.\n",
            "COPY public.entries (id) FROM stdin;\n\\.\n",
            "COPY public.facts (id) FROM stdin;\r\n1\r\n\\.\r\n",
            "-- PostgreSQL database dump complete\n",
        );
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(text.as_bytes()).unwrap();
        let file = Scratch::new(&encoder.finish().unwrap());
        let (verdict, counts) = complete_with_counts(&file.0);
        assert_eq!(verdict, Verdict::Pass);
        assert_eq!(
            counts.into_iter().collect::<Vec<_>>(),
            [
                ("public.entries".to_owned(), 0),
                ("public.facts".to_owned(), 1),
                ("public.meta".to_owned(), 2)
            ]
        );
    }

    #[test]
    fn lines_follow_python_text_mode() {
        // Each expectation was observed from the Python oracle's
        // `_dump_complete` on the same bytes (2026-11-13, CPython 3.11).
        let m = "-- PostgreSQL database dump complete";
        for (text, expected) in [
            (format!("SELECT 1;\n{m}\n"), Verdict::Pass),
            ("SELECT 1;\n".to_owned(), Verdict::Refuse),
            (format!("x\r\n{m}\r\n"), Verdict::Pass),
            (format!("x\r{m}\r"), Verdict::Pass),
            (format!("x\n{m}"), Verdict::Pass),
            (format!("{m} \n"), Verdict::Refuse),
            (format!("{m}\0\n"), Verdict::Refuse),
            (format!("x\x0b{m}\n"), Verdict::Refuse),
            (format!("x\u{2028}{m}\n"), Verdict::Refuse),
            (format!("COPY t (a) FROM stdin;\n{m}\n"), Verdict::Refuse),
            (
                format!("COPY t (a) FROM stdin;\n{m}\n\\.\n{m}\n"),
                Verdict::Pass,
            ),
            (format!("COPY  FROM stdin;\n{m}\n"), Verdict::Pass),
            (format!("COPY tFROM stdin;\n{m}\n"), Verdict::Pass),
            (format!("COPY t\tFROM stdin;\n{m}\n"), Verdict::Pass),
            (format!("COPY t\x1c FROM stdin;\n{m}\n"), Verdict::Pass),
            (format!("COPY t FROM stdin;\r\n{m}\r\n"), Verdict::Refuse),
            (format!("COPY t\u{a0}x FROM stdin;\n{m}\n"), Verdict::Pass),
        ] {
            assert_eq!(verdict(text.as_bytes()), expected, "{text:?}");
        }
        let mut invalid = b"\xff\xfe\n".to_vec();
        invalid.extend_from_slice(m.as_bytes());
        invalid.push(b'\n');
        assert_eq!(verdict(&invalid), Verdict::Pass);
        let mut marker_invalid = m.as_bytes().to_vec();
        marker_invalid.extend_from_slice(b"\xff\n");
        assert_eq!(verdict(&marker_invalid), Verdict::Refuse);
        let mut copy_invalid = b"COPY \xff FROM stdin;\n".to_vec();
        copy_invalid.extend_from_slice(m.as_bytes());
        assert_eq!(verdict(&copy_invalid), Verdict::Refuse);
    }

    #[test]
    fn a_missing_file_or_directory_is_incomplete() {
        let missing = std::env::temp_dir().join("pl-restore-missing-dump.sql.gz");
        assert_eq!(complete(&missing), Verdict::Refuse);
        assert_eq!(complete(&std::env::temp_dir()), Verdict::Refuse);
    }
}
