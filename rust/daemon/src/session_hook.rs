//! The plugin's SessionStart hook texts (`web/session_hook.py`): the main
//! hook (`hook_session_start`: notices, the session's registration and its
//! episode handle, the startup memory policy, the daemon-side custom
//! instructions, onboarding and the bounded briefing) and the separate
//! memory-policy hook (`hook_memory_policy`).

use crate::config::MemoryPolicyConfig;
use crate::http::App;
use crate::service::Ready;
use sha2::{Digest, Sha256};
use std::sync::Arc;

/// `HOOK_CONTEXT_MAX_CHARS`: one hook output's budget, in UTF-8 bytes.
const HOOK_CONTEXT_MAX_CHARS: usize = 9_500;

/// `MEMORY_POLICY_VARIANTS`.
const MEMORY_POLICY_VARIANTS: [&str; 3] = ["none", "compact", "full_separate_hook"];

/// `CONTINUED_SOURCES`: SessionStart sources that continue a conversation.
const CONTINUED_SOURCES: [&str; 2] = ["resume", "compact"];

/// `MEMORY_LOOP_BLOCK`, recorded byte for byte from Python
/// (`harness/record_routes.py`).
const MEMORY_LOOP_BLOCK: &str = include_str!("../harness/goldens/memory-loop-block.txt");

/// `STARTUP_MEMORY_CORE`, the `compact` variant's policy text, recorded
/// byte for byte from Python (`harness/record_routes.py`).
const STARTUP_MEMORY_CORE: &str = include_str!("../harness/goldens/startup-memory-core.txt");

/// `ONBOARDING_BLOCK`, served when the bank is provably empty, recorded
/// byte for byte from Python (`harness/record_routes.py`).
const ONBOARDING_BLOCK: &str = include_str!("../harness/goldens/onboarding-block.txt");

/// `UPDATE_COMMAND`: what the notices name when the hook reported no
/// launcher path.
const UPDATE_COMMAND: &str = "pseudolife-mcp";

/// `DAEMON_VERSION`.
const DAEMON_VERSION: &str = env!("CARGO_PKG_VERSION");

/// `ab_arm_index`: SHA-256 of the session id, its first 8 bytes big-endian,
/// modulo the arm count.
fn ab_arm_index(session_id: &str, arms: usize) -> usize {
    let digest = Sha256::digest(session_id.as_bytes());
    let head = u64::from_be_bytes(digest[..8].try_into().expect("8 bytes"));
    if crate::mutants::active("policy-arm-shifted") {
        return ((head + 1) % arms as u64) as usize;
    }
    (head % arms as u64) as usize
}

/// `memory_policy_variant`: the session's A/B arm when two or more arms are
/// set and it has an id, else the configured variant; `compact` for
/// anything unknown.
pub fn memory_policy_variant(cfg: &MemoryPolicyConfig, session_id: Option<&str>) -> String {
    let variant = match session_id {
        Some(id) if !id.is_empty() && cfg.ab_arms.len() >= 2 => {
            cfg.ab_arms[ab_arm_index(id, cfg.ab_arms.len())].clone()
        }
        _ => cfg.variant.clone(),
    };
    if MEMORY_POLICY_VARIANTS.contains(&variant.as_str()) {
        variant
    } else {
        "compact".into()
    }
}

/// `hook_memory_policy`: the full memory-loop block for a session whose
/// variant is `full_separate_hook`, else empty; nothing on a resume or
/// compaction.
pub fn hook_memory_policy(
    cfg: &MemoryPolicyConfig,
    session_id: Option<&str>,
    source: Option<&str>,
) -> &'static str {
    if is_continued(source) {
        return "";
    }
    if memory_policy_variant(cfg, session_id) != "full_separate_hook" {
        return "";
    }
    if MEMORY_LOOP_BLOCK.len() <= HOOK_CONTEXT_MAX_CHARS {
        MEMORY_LOOP_BLOCK
    } else {
        ""
    }
}

fn is_continued(source: Option<&str>) -> bool {
    source.is_some_and(|s| CONTINUED_SOURCES.contains(&s))
}

// ---------------------------------------------------------------------------
// Notices
// ---------------------------------------------------------------------------

/// `_LAUNCHER_SHAPE` (fullmatch): an absolute path, in plain path
/// characters, to a file named `pseudolife-mcp` or `pseudolife-mcp.exe`.
fn launcher_shape(s: &str) -> bool {
    let b = s.as_bytes();
    let sep = |c: u8| c == b'\\' || c == b'/';
    let prefix = if b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && sep(b[2]) {
        3
    } else if b.first().copied().is_some_and(|c| c == b'/') {
        1
    } else {
        return false;
    };
    let name = if s.ends_with("pseudolife-mcp.exe") {
        "pseudolife-mcp.exe".len()
    } else if s.ends_with("pseudolife-mcp") {
        "pseudolife-mcp".len()
    } else {
        return false;
    };
    // The separator before the name, then the middle between the two.
    if prefix + 1 + name > b.len() || !sep(b[b.len() - name - 1]) {
        return false;
    }
    let middle = &b[prefix..b.len() - name - 1];
    middle.len() <= 250
        && middle
            .iter()
            .all(|&c| c.is_ascii_alphanumeric() || b" _.~()+,@\\/-".contains(&c))
}

/// `launcher_command`: the launcher path the hook reported, quoted when it
/// holds a space, else `pseudolife-mcp`.
fn launcher_command(launcher: Option<&str>) -> String {
    match launcher.filter(|l| launcher_shape(l)) {
        Some(l) if l.contains(' ') => format!("\"{l}\""),
        Some(l) => l.to_string(),
        None => UPDATE_COMMAND.to_string(),
    }
}

/// `_checkout_built`: the daemon was built from a checkout
/// (`PSEUDOLIFE_BUILD_SOURCE=checkout`).
fn checkout_built() -> bool {
    std::env::var("PSEUDOLIFE_BUILD_SOURCE")
        .is_ok_and(|v| crate::board::text::py_strip(&v).to_lowercase() == "checkout")
}

fn plugin_update_commands(command: &str) -> String {
    format!(
        "{command} update --clients-only --tag {DAEMON_VERSION}, or in Claude Code \
         /plugin marketplace update pseudolife-mcp, then \
         /plugin update pseudolife-memory@pseudolife-mcp"
    )
}

fn daemon_update_commands(command: &str) -> String {
    format!("{command} update, or from a checkout git pull, then ops/update.ps1 or ops/update.sh")
}

/// `all_update_commands`: a checkout-built daemon names the checkout
/// command first.
fn all_update_commands(command: &str, checkout_built: bool) -> String {
    let release = format!("{command} update --clients-only --tag {DAEMON_VERSION}");
    let checkout = "git pull, then ops/update.ps1 -All on Windows or ops/update.sh --all";
    if checkout_built {
        format!(
            "in the checkout this daemon was built from: {checkout} (on a release install: {release})"
        )
    } else {
        format!("{release}, or from a checkout {checkout}")
    }
}

/// `_DIGEST_SHAPE` (fullmatch): 64 lowercase hex characters.
fn digest_shape(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

/// `hooks_notice`: one line when the plugin is the daemon's version but
/// its hook scripts are not the daemon's, else empty.
fn hooks_notice(
    plugin_version: Option<&str>,
    plugin_digest: Option<&str>,
    daemon_digest: Option<&str>,
    command: &str,
    checkout_built: bool,
) -> String {
    let Some(version) = plugin_version.filter(|v| !v.is_empty() && *v == DAEMON_VERSION) else {
        return String::new();
    };
    let (Some(plugin), Some(daemon)) = (plugin_digest, daemon_digest) else {
        return String::new();
    };
    if !digest_shape(plugin) || !digest_shape(daemon) || plugin == daemon {
        return String::new();
    }
    format!(
        "Pseudolife-MCP: plugin {version} matches the daemon's version but its hooks differ \
         from the daemon's copy — run {} to refresh the plugin cache and shim, then start a \
         new session.",
        all_update_commands(command, checkout_built)
    )
}

/// `version_notice`: one line when the plugin release differs from the
/// daemon's, naming the side that is behind when the versions say.
fn version_notice(plugin_version: Option<&str>, command: &str) -> String {
    use crate::release_check::{compare_keys, version_key, version_shape};
    use std::cmp::Ordering;
    let Some(plugin) = plugin_version.filter(|v| !v.is_empty() && version_shape(v)) else {
        return String::new();
    };
    if plugin == DAEMON_VERSION {
        return String::new();
    }
    let head = format!("Pseudolife-MCP: plugin {plugin} and daemon {DAEMON_VERSION} differ");
    let order = match (version_key(plugin), version_key(DAEMON_VERSION)) {
        (Some(p), Some(d)) => compare_keys(&p, &d),
        _ => Ordering::Equal,
    };
    match order {
        Ordering::Less => format!(
            "{head} — update the plugin ({}) and start a new session; until then its hooks \
             may lack what the daemon serves.",
            plugin_update_commands(command)
        ),
        Ordering::Greater => format!(
            "{head} — update the daemon ({}); until then the plugin may call what the daemon \
             does not serve.",
            daemon_update_commands(command)
        ),
        Ordering::Equal => format!(
            "{head} — update the older one: plugin via {}; daemon via {}.",
            plugin_update_commands(command),
            daemon_update_commands(command)
        ),
    }
}

// ---------------------------------------------------------------------------
// The startup body
// ---------------------------------------------------------------------------

fn byte_len(parts: &[String]) -> i64 {
    parts.join("\n\n").len() as i64
}

/// `_custom_instructions`: `<data_dir>/hook-instructions.md`, read as
/// Python's text mode reads it (strict UTF-8, universal newlines), then
/// stripped; empty when absent or unreadable.
fn custom_instructions(app: &App) -> String {
    let path = app.service.data_dir.join("hook-instructions.md");
    if !path.is_file() {
        return String::new();
    }
    let Ok(raw) = std::fs::read(&path) else {
        return String::new();
    };
    let Ok(text) = String::from_utf8(raw) else {
        return String::new();
    };
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    crate::board::text::py_strip(&text).to_string()
}

/// `re.split(r"\n\s*\n", text)`: split at each newline-whitespace-newline
/// run, the match running to the run's last newline.
fn split_paragraphs(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let (mut start, mut i) = (0, 0);
    while i < chars.len() {
        if chars[i] == '\n' {
            let mut j = i + 1;
            let mut last = None;
            while j < chars.len() && crate::board::text::py_space(chars[j]) {
                if chars[j] == '\n' {
                    last = Some(j);
                }
                j += 1;
            }
            if let Some(end) = last {
                out.push(chars[start..i].iter().collect());
                start = end + 1;
                i = end + 1;
                continue;
            }
        }
        i += 1;
    }
    out.push(chars[start..].iter().collect());
    out
}

/// `_bounded_custom_instructions`: whole paragraphs within the budget, and
/// a warning when any were left out.
fn bounded_custom_instructions(text: &str, max_bytes: i64) -> String {
    let blocks: Vec<String> = split_paragraphs(text)
        .iter()
        .map(|b| crate::board::text::py_strip(b).to_string())
        .filter(|b| !b.is_empty())
        .collect();
    if blocks.is_empty() || max_bytes <= 0 {
        return String::new();
    }
    let warning = "Custom instructions are partial. Obtain the complete daemon-side \
                   `<data_dir>/hook-instructions.md` before relying on this override.";
    let pack = |cap: i64| -> Vec<String> {
        let mut chosen: Vec<String> = Vec::new();
        for block in &blocks {
            let mut candidate = chosen.clone();
            candidate.push(block.clone());
            if byte_len(&candidate) <= cap {
                chosen.push(block.clone());
            }
        }
        chosen
    };
    let chosen = pack(max_bytes);
    if chosen.len() == blocks.len() {
        return chosen.join("\n\n");
    }
    let chosen = pack(max_bytes - warning.len() as i64 - 2);
    let rendered = chosen.join("\n\n");
    let candidate = if rendered.is_empty() {
        warning.to_string()
    } else {
        format!("{rendered}\n\n{warning}")
    };
    if candidate.len() as i64 <= max_bytes {
        candidate
    } else {
        String::new()
    }
}

/// `_cold_bank`: true only when the bank is provably empty (`stats()`'s
/// `total_memories`: every band's entries plus the reference chunks); any
/// doubt means warm.
async fn cold_bank(app: &App, ready: &Ready) -> bool {
    let specs = &app.service.config.memory.band_specs;
    let entries: usize = {
        let bank = ready.bank.read().expect("bank lock");
        specs
            .iter()
            .enumerate()
            .filter(|(i, s)| specs.iter().rposition(|o| o.name == s.name) == Some(*i))
            .map(|(_, s)| bank.entries.iter().filter(|e| e.band == s.name).count())
            .sum()
    };
    if specs.is_empty() || entries > 0 {
        return false;
    }
    let chunks = crate::txn::with_client(ready.storage.client(), |db| async move {
        let row = db
            .query_one("SELECT count(*) FROM reference_chunks", &[])
            .await?;
        Ok::<_, tokio_postgres::Error>(row.get::<_, i64>(0))
    })
    .await;
    matches!(chunks, Ok(0))
}

/// `session_start_context`: the variant's public policy text; private
/// content (custom instructions, onboarding, the briefing) only for an
/// authorized caller.
async fn session_start_context(
    app: &App,
    ready: Option<&Arc<Ready>>,
    authorized: bool,
    session_id: Option<&str>,
    max_bytes: i64,
) -> String {
    let variant = memory_policy_variant(&app.service.config.memory_policy, session_id);
    let policy = if variant == "compact" {
        STARTUP_MEMORY_CORE
    } else {
        ""
    };
    if policy.len() as i64 > max_bytes {
        let short = "Memory: call `memory_search` and `memory_lesson_search` at task start.";
        return if short.len() as i64 <= max_bytes {
            short.into()
        } else {
            String::new()
        };
    }
    let mut parts: Vec<String> = Vec::new();
    if !policy.is_empty() {
        parts.push(policy.to_string());
    }
    let remaining = |parts: &[String]| max_bytes - byte_len(parts) - 2;
    let add = |parts: &mut Vec<String>, text: String| {
        if !text.is_empty() && text.len() as i64 <= remaining(parts) {
            parts.push(text);
        }
    };
    if authorized {
        let custom = custom_instructions(app);
        if !custom.is_empty() {
            let cap = 3_500.min(remaining(&parts) - 2_000);
            add(&mut parts, bounded_custom_instructions(&custom, cap));
        }
        if let Some(ready) = ready {
            if variant != "none" && cold_bank(app, ready).await {
                add(&mut parts, ONBOARDING_BLOCK.to_string());
            }
            let md = if crate::mutants::active("session-start-no-briefing") {
                String::new()
            } else {
                crate::briefing::session_briefing(
                    ready,
                    &app.service.config,
                    &crate::briefing::HOOK_OPTIONS,
                )
                .await
                .ok()
                .and_then(|b| b.get("markdown").and_then(|m| m.as_str()).map(String::from))
                .unwrap_or_default()
            };
            if !crate::board::text::py_strip(&md).is_empty() {
                let room = remaining(&parts);
                add(
                    &mut parts,
                    crate::briefing::format_bounded_briefing(&md, room),
                );
            }
        }
    }
    parts.join("\n\n")
}

/// `_continued_context`: what a resumed or compacted session still needs:
/// one pointer line and, after a compaction, the daemon-side custom
/// instructions.
fn continued_context(
    app: &App,
    source: &str,
    authorized: bool,
    has_handle: bool,
    max_bytes: i64,
) -> String {
    let kind = if source == "resume" {
        "resumed"
    } else {
        "compacted"
    };
    let handle = if has_handle {
        "Keep passing the episode handle above to every tool that accepts it. "
    } else {
        ""
    };
    let note = format!(
        "Pseudolife-MCP: {kind} session, so the startup memory briefing is not re-sent. \
         {handle}Recall with `memory_search` and `memory_lesson_search`; the full briefing \
         is `pseudolife-mcp briefing` or GET /api/briefing."
    );
    let mut parts: Vec<String> = Vec::new();
    if note.len() as i64 <= max_bytes {
        parts.push(note);
    }
    if authorized && source == "compact" {
        let custom = custom_instructions(app);
        if !custom.is_empty() {
            let room = max_bytes - byte_len(&parts) - 2;
            let text = bounded_custom_instructions(&custom, 3_500.min(room));
            if !text.is_empty() {
                parts.push(text);
            }
        }
    }
    parts.join("\n\n")
}

/// `_episode_advertisement`: register the session (its episode, the
/// `client_sessions` row with the policy variant served, the active-session
/// pointer) and return the episode-handle line; empty on any failure.
async fn episode_advertisement(
    app: &App,
    ready: &Arc<Ready>,
    session_id: &str,
    principal: Option<&str>,
) -> String {
    let variant = memory_policy_variant(&app.service.config.memory_policy, Some(session_id));
    let (r2, id, principal) = (
        ready.clone(),
        session_id.to_string(),
        principal.map(str::to_string),
    );
    let task = ready.writes.spawn(async move {
        let reg = crate::session_register::Registration {
            via: crate::session_register::RegisteredVia::Hook,
            principal: principal.as_deref(),
            policy_variant: Some(&variant),
        };
        let ep = crate::session_register::episode_start_session(
            &r2,
            Some(&id),
            crate::episodes::generic_title(),
            None,
            reg,
        )
        .await?;
        crate::session_register::set_active_session(&r2, &id).await?;
        Ok::<_, String>(ep)
    });
    let ep = match task.await {
        Ok(Ok(ep)) => ep,
        Ok(Err(e)) => {
            eprintln!("session-start identity registration failed: {e}");
            return String::new();
        }
        Err(e) => {
            eprintln!("session-start identity registration failed: {e}");
            return String::new();
        }
    };
    let short: String = ep
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .chars()
        .take(12)
        .collect();
    if short.is_empty() {
        return String::new();
    }
    format!(
        "Session episode: {short} — pass episode=\"{short}\" to every memory tool that accepts \
         it, including memory_episode_start/end and memory_session_title (keeps attribution \
         correct even when other sessions are open)."
    )
}

/// The query of `GET /api/hook/session-start`.
pub struct SessionStartQuery<'a> {
    /// The caller's principal name, when authorized.
    pub principal: Option<&'a str>,
    pub session_id: Option<&'a str>,
    pub source: Option<&'a str>,
    pub plugin_version: Option<&'a str>,
    pub plugin_hooks_digest: Option<&'a str>,
    pub launcher: Option<&'a str>,
}

/// `hook_session_start`: the notices (a newer release, else a plugin
/// version mismatch, else a hooks mismatch), for an authorized caller the
/// dream-stall and (fresh starts only) review-queue lines, the session's
/// registration and handle line, then the startup body, or the continued
/// context on a resume or compaction. Never fails.
pub async fn hook_session_start(app: &App, q: SessionStartQuery<'_>) -> String {
    let authorized = q.principal.is_some();
    let command = launcher_command(q.launcher);
    let latest = app.service.release_check.snapshot().latest_release;
    let daemon_digest = crate::health::hooks_digest(app.service.env.plugin_dir.as_deref());
    let mut notice = crate::release_check::update_notice(
        DAEMON_VERSION,
        latest.as_deref(),
        q.plugin_version,
        &command,
    );
    if notice.is_empty() {
        notice = version_notice(q.plugin_version, &command);
    }
    if notice.is_empty() {
        notice = hooks_notice(
            q.plugin_version,
            q.plugin_hooks_digest,
            daemon_digest.as_deref(),
            &command,
            checkout_built(),
        );
    }
    let mut prefix: Vec<String> = Vec::new();
    if !notice.is_empty() {
        prefix.push(notice);
    }
    // Everything past the notices reads the bank, and only for an
    // authorized caller; a bank that cannot open leaves those parts out, as
    // Python's raised reads do.
    // A resumed or compacted start with no session id reads nothing but the
    // stall tracker, which Python keeps without opening the bank.
    let needs_bank = q.session_id.is_some_and(|s| !s.is_empty()) || !is_continued(q.source);
    let ready = if !authorized {
        None
    } else if needs_bank {
        app.service.ensure_init().await.ok()
    } else {
        app.service.ready_if_initialized().await
    };
    if authorized && let Some(ready) = &ready {
        let stall = crate::briefing::dream_stall_line(&crate::briefing::current_stall(ready));
        if !stall.is_empty() {
            prefix.push(stall);
        }
        if !is_continued(q.source) {
            let health = crate::dream::service::review_queue_health(
                ready,
                &app.service.config,
                crate::episodes::now(),
                false,
            )
            .await;
            let queue = crate::briefing::review_queue_line(&health);
            if !queue.is_empty() {
                prefix.push(queue);
            }
        }
    }
    let mut has_handle = false;
    if let (Some(id), Some(ready)) = (q.session_id.filter(|s| !s.is_empty()), &ready) {
        let ad = episode_advertisement(app, ready, id, q.principal).await;
        if !ad.is_empty() {
            prefix.push(ad);
            has_handle = true;
            let variant = memory_policy_variant(&app.service.config.memory_policy, Some(id));
            eprintln!("memory-policy variant {variant} for a registered session");
        }
    }
    let prefix = prefix.join("\n\n");
    let budget =
        HOOK_CONTEXT_MAX_CHARS as i64 - prefix.len() as i64 - if prefix.is_empty() { 0 } else { 2 };
    let body = match q.source.filter(|s| CONTINUED_SOURCES.contains(s)) {
        Some(source) => continued_context(app, source, authorized, has_handle, budget),
        None => {
            let session = if authorized { q.session_id } else { None };
            session_start_context(app, ready.as_ref(), authorized, session, budget).await
        }
    };
    if !prefix.is_empty() && !body.is_empty() {
        format!("{prefix}\n\n{body}")
    } else {
        prefix + &body
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(variant: &str, arms: &[&str]) -> MemoryPolicyConfig {
        MemoryPolicyConfig {
            variant: variant.into(),
            ab_arms: arms.iter().map(|a| a.to_string()).collect(),
        }
    }

    #[test]
    fn arms_follow_the_python_hash() {
        // `int.from_bytes(sha256(id)[:8], "big") % 3`, computed in Python.
        assert_eq!(ab_arm_index("sess-a", 3), 1);
        assert_eq!(ab_arm_index("sess-b", 3), 2);
        assert_eq!(ab_arm_index("sess-c", 3), 0);
        assert_eq!(ab_arm_index("sess-c", 2), 1);
    }

    #[test]
    fn the_block_serves_only_the_separate_hook_variant() {
        let full = cfg("full_separate_hook", &[]);
        assert_eq!(hook_memory_policy(&full, None, None), MEMORY_LOOP_BLOCK);
        assert_eq!(hook_memory_policy(&full, Some("x"), Some("resume")), "");
        assert_eq!(hook_memory_policy(&full, Some("x"), Some("compact")), "");
        assert_eq!(
            hook_memory_policy(&full, Some("x"), Some("startup")),
            MEMORY_LOOP_BLOCK
        );
        assert_eq!(hook_memory_policy(&cfg("compact", &[]), None, None), "");
        assert!(MEMORY_LOOP_BLOCK.len() < HOOK_CONTEXT_MAX_CHARS);
        assert!(!MEMORY_LOOP_BLOCK.contains('\r'));
    }

    #[test]
    fn a_session_id_picks_its_arm_only_with_two_or_more_arms() {
        let arms = cfg("compact", &["none", "compact", "full_separate_hook"]);
        assert_eq!(
            memory_policy_variant(&arms, Some("sess-b")),
            "full_separate_hook"
        );
        assert_eq!(memory_policy_variant(&arms, Some("sess-c")), "none");
        assert_eq!(memory_policy_variant(&arms, None), "compact");
        assert_eq!(memory_policy_variant(&arms, Some("")), "compact");
        assert_eq!(memory_policy_variant(&cfg("bogus", &[]), None), "compact");
    }

    #[test]
    fn the_recorded_texts_are_lf_only() {
        for text in [STARTUP_MEMORY_CORE, ONBOARDING_BLOCK] {
            assert!(!text.contains('\r'));
            assert!(!text.ends_with('\n'));
        }
    }

    #[test]
    fn launcher_paths_follow_the_python_shape() {
        // `_LAUNCHER_SHAPE.fullmatch`, checked in Python.
        for ok in [
            "D:\\Tools\\Pseudolife (x64)\\bin\\pseudolife-mcp.exe",
            "/home/me/.local/bin/pseudolife-mcp",
            "//pseudolife-mcp",
            "C:/x y/pseudolife-mcp",
        ] {
            assert!(launcher_shape(ok), "{ok}");
        }
        for bad in [
            "pseudolife-mcp",
            "/pseudolife-mcp",
            "C:pseudolife-mcp",
            "/bin/pseudolife-mcp.exe.bak",
            "/bin/evil;rm/pseudolife-mcp",
            "/bin/pseudolife-mcp\n",
            "relative/pseudolife-mcp",
        ] {
            assert!(!launcher_shape(bad), "{bad}");
        }
        assert_eq!(launcher_command(None), "pseudolife-mcp");
        assert_eq!(
            launcher_command(Some("C:/x y/pseudolife-mcp")),
            "\"C:/x y/pseudolife-mcp\""
        );
        assert_eq!(launcher_command(Some("nope")), "pseudolife-mcp");
        let long = format!("/{}/pseudolife-mcp", "a".repeat(251));
        assert!(!launcher_shape(&long));
        let fits = format!("/{}/pseudolife-mcp", "a".repeat(250));
        assert!(launcher_shape(&fits));
    }

    /// `version_notice`, `hooks_notice` and `all_update_commands` with
    /// command `pl`, computed in Python at daemon version 0.19.0.
    #[test]
    fn notices_follow_python() {
        assert_eq!(DAEMON_VERSION, "0.19.0", "recompute these from Python");
        let plugin_cmds = "pl update --clients-only --tag 0.19.0, or in Claude Code /plugin \
                           marketplace update pseudolife-mcp, then /plugin update \
                           pseudolife-memory@pseudolife-mcp";
        let daemon_cmds = "pl update, or from a checkout git pull, then ops/update.ps1 or \
                           ops/update.sh";
        assert_eq!(
            version_notice(Some("0.1.0"), "pl"),
            format!(
                "Pseudolife-MCP: plugin 0.1.0 and daemon 0.19.0 differ — update the plugin \
                 ({plugin_cmds}) and start a new session; until then its hooks may lack what \
                 the daemon serves."
            )
        );
        assert_eq!(
            version_notice(Some("99.0.0"), "pl"),
            format!(
                "Pseudolife-MCP: plugin 99.0.0 and daemon 0.19.0 differ — update the daemon \
                 ({daemon_cmds}); until then the plugin may call what the daemon does not serve."
            )
        );
        for equal_or_unordered in ["0.19.0.dev1", "abc"] {
            assert_eq!(
                version_notice(Some(equal_or_unordered), "pl"),
                format!(
                    "Pseudolife-MCP: plugin {equal_or_unordered} and daemon 0.19.0 differ — \
                     update the older one: plugin via {plugin_cmds}; daemon via {daemon_cmds}."
                )
            );
        }
        for silent in ["0.19.0", "", "bad version!", &"1".repeat(33)] {
            assert_eq!(version_notice(Some(silent), "pl"), "", "{silent:?}");
        }
        assert_eq!(version_notice(None, "pl"), "");

        let release = "pl update --clients-only --tag 0.19.0, or from a checkout git pull, \
                       then ops/update.ps1 -All on Windows or ops/update.sh --all";
        let checkout = "in the checkout this daemon was built from: git pull, then \
                        ops/update.ps1 -All on Windows or ops/update.sh --all (on a release \
                        install: pl update --clients-only --tag 0.19.0)";
        assert_eq!(all_update_commands("pl", false), release);
        assert_eq!(all_update_commands("pl", true), checkout);
        let (d, b) = ("a".repeat(64), "b".repeat(64));
        let hooks = |cmds: &str| {
            format!(
                "Pseudolife-MCP: plugin 0.19.0 matches the daemon's version but its hooks \
                 differ from the daemon's copy — run {cmds} to refresh the plugin cache and \
                 shim, then start a new session."
            )
        };
        let v = Some("0.19.0");
        assert_eq!(
            hooks_notice(v, Some(&b), Some(&d), "pl", false),
            hooks(release)
        );
        assert_eq!(
            hooks_notice(v, Some(&b), Some(&d), "pl", true),
            hooks(checkout)
        );
        assert_eq!(hooks_notice(v, Some(&d), Some(&d), "pl", false), "");
        assert_eq!(
            hooks_notice(Some("0.1.0"), Some(&b), Some(&d), "pl", false),
            ""
        );
        let upper = "B".repeat(64);
        assert_eq!(hooks_notice(v, Some(&upper), Some(&d), "pl", false), "");
        assert_eq!(hooks_notice(v, Some(&b), None, "pl", false), "");
    }

    #[test]
    fn paragraphs_split_as_python_re_split() {
        // re.split(r"\n\s*\n", text)
        assert_eq!(split_paragraphs("a\n\nb"), ["a", "b"]);
        assert_eq!(split_paragraphs("a\n \t\n\n c"), ["a", " c"]);
        assert_eq!(split_paragraphs("a\nb"), ["a\nb"]);
        assert_eq!(split_paragraphs("a\n\x1f\nb\n"), ["a", "b\n"]);
        assert_eq!(split_paragraphs("\n\na"), ["", "a"]);
    }
}
