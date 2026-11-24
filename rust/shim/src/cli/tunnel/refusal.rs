//! The refusal for a command that met a value or a store file in a form
//! this client does not read: it names the option or the file and the fix.
use std::path::Path;

use super::{
    ChallengeIssue, Command, Fail, Options, challenge_issue, challenge_path, private_object, store,
    syntax,
};

/// Names the setup option, or the first file of `options.profile`, this
/// client could not read, in the order the commands read them, and says
/// what to do about it. Values are named by their option, never echoed.
pub(super) fn unread(options: &Options, root: &Path) -> String {
    let name = &options.profile;
    let word = options.command.word();
    if options.command == Command::Setup {
        let setup = &options.setup;
        if setup
            .key_expires_at
            .as_deref()
            .is_some_and(|value| matches!(syntax::parse_expiry(value), syntax::Admit::Defer))
        {
            return "--key-expires-at is an expiry spelling this client does not read; give an ISO 8601 time with a timezone, such as 2026-12-01T00:00:00Z, or a date such as 2026-12-01".to_owned();
        }
        if setup
            .daemon_url
            .as_deref()
            .is_some_and(|url| syntax::validate_url(url) == Err(Fail::Defer))
        {
            return "the URL in --daemon-url is a spelling this client does not accept; use http://127.0.0.1:<port> or http://localhost:<port> in lower case, or an https:// URL with a plain host name".to_owned();
        }
        if setup
            .token_file
            .as_deref()
            .is_some_and(|file| store::checked_path(Path::new(file)) == Err(Fail::Defer))
        {
            return "--token-file is a path form this client does not read (a UNC or device path); give a local absolute path".to_owned();
        }
    }
    if matches!(store::load(root, name), Err(Fail::Defer)) {
        return format!(
            "this client could not read profile {}: {}. Move that file aside and save the profile again with `pseudolife-mcp tunnel setup`, then retry tunnel {word}",
            store::profile_file(root, name).display(),
            store::unread_reason(root, name)
        );
    }
    if matches!(store::key_state(root, name), Err(Fail::Defer)) {
        return format!(
            "cannot read the key file {}; check that it is a plain owner-only file, not a pipe or a link, then retry tunnel {word}",
            store::key_path(root, name).display()
        );
    }
    match challenge_issue(root, name) {
        Some(ChallengeIssue::Form) => {
            return format!(
                "cannot read the verification challenge {}; check that it is a plain owner-only file, not a pipe or a link, then retry tunnel {word}",
                challenge_path(root, name).display()
            );
        }
        Some(ChallengeIssue::Deep) => {
            return format!(
                "the verification challenge {} is nested too deeply to be one this client wrote; move that file aside (tunnel verify writes a new one), then retry tunnel {word}",
                challenge_path(root, name).display()
            );
        }
        None => {}
    }
    for file in [
        format!("{name}.reload.json"),
        format!("{name}.reload.result.json"),
    ] {
        let path = root.join(file);
        if matches!(private_object(&path), Err(Fail::Defer)) {
            return format!(
                "cannot read the refresh file {}: it is not a plain owner-only file, or holds JSON this client does not read. Move it aside, then retry tunnel {word}",
                path.display()
            );
        }
    }
    if store::occupied(root).unwrap_or(true) && matches!(store::list(root), Err(Fail::Defer)) {
        return store::list_refusal(root);
    }
    format!(
        "tunnel {word} met a file in {} that this client cannot read (a link, a junction, a pipe, or a name or record it cannot decode). `pseudolife-mcp tunnel update` names each such file; move it aside, then retry",
        root.display()
    )
}
