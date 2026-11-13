//! The standing-instructions fallback: the memory block appended to Codex's
//! `AGENTS.md` (or a non-empty `AGENTS.override.md`, which Codex reads
//! first) when hooks cannot deliver it, never twice.
use super::{Fail, Report};
use std::path::{Path, PathBuf};

const MARKERS: [&[u8]; 5] = [
    b"## Memory",
    b"pseudolife-memory",
    b"RECALL",
    b"CAPTURE",
    b"REFLECT",
];

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

/// The file Codex reads standing instructions from: a non-empty
/// `AGENTS.override.md` first, else `AGENTS.md`.
fn target(home: &Path) -> PathBuf {
    let override_path = home.join("AGENTS.override.md");
    if std::fs::read_to_string(&override_path).is_ok_and(|t| !t.trim().is_empty()) {
        override_path
    } else {
        home.join("AGENTS.md")
    }
}

/// "present", "skipped", "covered-by-hooks" or "appended" (the states both
/// installers print); on failure, the file that was not written and why.
pub fn standing(
    home: &Path,
    checkout: &Path,
    choice: &str,
    fallback_allowed: bool,
    ready: bool,
    report: &mut Report,
) -> Result<&'static str, (PathBuf, Fail)> {
    let path = target(home);
    write(
        &path,
        home,
        checkout,
        choice,
        fallback_allowed,
        ready,
        report,
    )
    .map_err(|error| (path, error))
}

fn write(
    path: &Path,
    home: &Path,
    checkout: &Path,
    choice: &str,
    fallback_allowed: bool,
    ready: bool,
    report: &mut Report,
) -> Result<&'static str, Fail> {
    let path = path.to_path_buf();
    let old = crate::runtime::registrations::read_optional(&path).map_err(|error| {
        Fail::detail(format!("{} could not be read ({error}).", path.display()))
    })?;
    let old_bytes = old.clone().unwrap_or_default();
    if MARKERS.iter().all(|m| contains(&old_bytes, m)) {
        return Ok("present");
    }
    if choice == "skip" {
        return Ok("skipped");
    }
    if choice == "auto" && ready {
        // Verified hooks serve a compact memory core instead.
        return Ok("covered-by-hooks");
    }
    if choice != "append" && !fallback_allowed {
        return Ok("skipped");
    }
    let block_path = checkout.join("examples").join("CLAUDE.memory.md");
    let block = std::fs::read(&block_path).map_err(|_| {
        Fail::detail(format!(
            "{} could not be read; pass --checkout with this repository's root.",
            block_path.display()
        ))
    })?;
    let mut data = old_bytes.clone();
    if !data.is_empty() {
        data.extend_from_slice(b"\n\n");
    }
    data.extend_from_slice(&block);
    if old.is_none() && !home.is_dir() {
        std::fs::create_dir_all(home).map_err(|_| {
            Fail::detail(format!(
                "The Codex home {} could not be created.",
                home.display()
            ))
        })?;
    }
    let backup = crate::cli::installer::codex_config::write_retrying(&path, &old_bytes, &data)
        .map_err(|error| {
            Fail::detail(format!(
                "{} could not be written ({error}); it was left as it was.",
                path.display()
            ))
        })?;
    if old.is_some() {
        report.backups.push(backup);
    }
    report.instructions_path = Some(path);
    Ok("appended")
}
