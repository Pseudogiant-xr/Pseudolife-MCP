//! `console_sessions.rs` against tests/test_console_signin.py and
//! tests/test_console_sessions_pg.py (the oracle's own cases), plus the
//! database operations on the test bank (`PL_W1A_TEST_DSN`).

use super::*;
use crate::auth::{EnvTokens, PrincipalStore, StoredPrincipal, parse_token_map};
use std::time::Instant;

const ENV_TOKEN: &str = "fixture-env-token";
const STORED_TOKEN: &str = "fixture-stored-token";
const SESSION_TOKEN: &str = "fixture-browser-session-token";
const STORE_SESSION_TOKEN: &str = "fixture-browser-session-from-paired";

fn sha(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

pub(crate) fn session(
    token: &str,
    principal: &str,
    source: &str,
    issuer: &str,
    sid: &str,
    created: f64,
    last_used: Option<f64>,
) -> ConsoleSession {
    ConsoleSession {
        session_id: sid.into(),
        token_hash: sha(token),
        principal: principal.into(),
        source: source.into(),
        issuer: issuer.into(),
        method: METHOD_LINK.into(),
        label: "Firefox on Linux".into(),
        created_at: created,
        last_used_at: last_used,
        expires_at: created + SESSION_MAX_S,
    }
}

fn env_session(now: f64) -> ConsoleSession {
    session(
        SESSION_TOKEN,
        "default",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "s1",
        now - 60.0,
        None,
    )
}

fn loaded() -> SessionSnapshot {
    let s = SessionSnapshot::new();
    s.finish_refresh(s.begin_refresh(), vec![], vec![]);
    s
}

fn env() -> EnvTokens {
    EnvTokens {
        single: Some(ENV_TOKEN.into()),
        map: vec![],
    }
}

fn loaded_store() -> PrincipalStore {
    let s = PrincipalStore::new();
    s.refresh(vec![], None, &[], Instant::now());
    s
}

fn stored(store: &PrincipalStore, name: &str, token: &str) {
    store.add(StoredPrincipal {
        principal: name.into(),
        token_hash: Some(sha(token)),
        tier: None,
        board: true,
        revoked: false,
        owned: true,
    });
}

fn resolve_with(
    s: &SessionSnapshot,
    token: &str,
    env: &EnvTokens,
    store: &PrincipalStore,
    now: f64,
) -> Result<Option<SessionHit>, Unavailable> {
    resolve(
        Some(&format!("Bearer {token}")),
        Some(s),
        &environment_issuers(env),
        &environment_principals(env),
        store,
        now,
    )
}

// -- origins ---------------------------------------------------------------------

#[test]
fn an_origin_is_normalised_as_a_browser_sends_it() {
    for (value, want) in [
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"),
        ("HTTP://LOCALHOST:8765", "http://localhost:8765"),
        ("https://bank.example.com:443", "https://bank.example.com"),
        ("http://localhost:80", "http://localhost"),
        (
            "https://bank.example.com:8443",
            "https://bank.example.com:8443",
        ),
        ("http://[::1]:8765", "http://[::1]:8765"),
        ("http://[::1]", "http://[::1]"),
        ("http://LocalHost?", "http://localhost"),
        ("https://bank.example.com:0", "https://bank.example.com:0"),
        // Probed against the oracle (CPython 3.11.17), 2026-11-03.
        ("http://[::1%eth0]:5", "http://[::1%eth0]:5"),
        ("http://[v1.x]", "http://v1.x"),
        ("http://b\u{fc}cher.example", "http://b\u{fc}cher.example"),
        (
            "http://\u{dc}n\u{ef}code.example:80",
            "http://\u{fc}n\u{ef}code.example",
        ),
        ("http://host:", "http://host"),
        ("https://host:443/", "https://host"),
        ("HTTPS://Host.Example.COM:0443", "https://host.example.com"),
        ("http://\u{130}x.example", "http://i\u{307}x.example"),
    ] {
        assert_eq!(normalize_origin(value).as_deref(), Some(want), "{value}");
    }
}

#[test]
fn anything_but_a_plain_origin_is_refused() {
    for value in [
        "",
        "ftp://host",
        "http://",
        "http://host/ui",
        "http://host?x=1",
        "http://host#x",
        "http://host#",
        "http://user:pw@host",
        "http://@host",
        "http://host:99999",
        "http://host:8x",
        "http://ho st",
        "http://host\t",
        "http://host\u{7f}",
        "127.0.0.1:8765",
        "localhost:8765",
        "http://[::1",
        "http://[localhost]",
        "http://[127.0.0.1]:80",
        "http://a[::1]",
        "http://[::1]x:80",
        "null",
        "http://ex\u{ff0f}ample.com",
        "http://host:+80",
    ] {
        assert_eq!(normalize_origin(value), None, "{value:?}");
    }
    assert_eq!(
        normalize_origin(&format!("http://{}", "a".repeat(250))),
        None
    );
}

#[test]
fn a_loopback_origin_is_keyed_as_localhost_whichever_alias_names_it() {
    for origin in [
        "http://127.0.0.1:8765",
        "http://[::1]:8765",
        "http://localhost:8765",
    ] {
        assert_eq!(origin_key(origin), "http://localhost:8765");
        assert!(is_loopback_origin(origin));
    }
    assert_eq!(origin_key("https://localhost"), "https://localhost");
    assert_eq!(
        origin_key("https://bank.example.com"),
        "https://bank.example.com"
    );
    assert!(!is_loopback_origin("http://100.64.0.1:8765"));
}

#[test]
fn route_ids_labels_and_codes() {
    assert!(valid_route_id("board") && valid_route_id(&format!("a{}", "b".repeat(31))));
    for bad in [
        "",
        "Board",
        "../evil",
        "1board",
        &format!("a{}", "b".repeat(32)),
    ] {
        assert!(!valid_route_id(bad), "{bad}");
    }
    assert_eq!(
        clean_label("  Firefox \u{1f} on\nLinux ").as_deref(),
        Some("Firefox on Linux")
    );
    assert_eq!(clean_label(""), Some(String::new()));
    assert_eq!(
        clean_label(&"x".repeat(64)).as_deref(),
        Some(&*"x".repeat(64))
    );
    assert_eq!(clean_label(&"x".repeat(65)), None);
    assert_eq!(
        clean_label("a\u{200b}b"),
        None,
        "a format character is not printable"
    );
    let code = new_code();
    assert_eq!(code.len(), 12);
    assert!(code.bytes().all(|b| PAIRING_CODE_ALPHABET.contains(&b)));
    assert_eq!(format_code("ABCDEFGHJKMN"), "ABCD-EFGH-JKMN");
    assert_eq!(new_session_id().len(), 16);
}

#[test]
fn a_bearer_is_hashed_as_utf8_then_latin1() {
    assert_eq!(bearer_hashes(Some("Bearer tok")), vec![sha("tok")]);
    assert!(bearer_hashes(Some("Basic tok")).is_empty() && bearer_hashes(None).is_empty());
    // The client sent UTF-8 "é"; the header arrives latin-1 decoded as "Ã©".
    let hashes = bearer_hashes(Some("Bearer caf\u{c3}\u{a9}"));
    assert_eq!(hashes, vec![sha("caf\u{c3}\u{a9}"), sha("café")]);
}

#[test]
fn environment_issuers_name_each_token_and_the_singular_one_default() {
    let e = EnvTokens {
        single: Some("one".into()),
        map: parse_token_map("two:alice,three:bob"),
    };
    let issuers = environment_issuers(&e);
    assert_eq!(
        issuers.get(&sha("one")).map(String::as_str),
        Some("default")
    );
    assert_eq!(issuers.get(&sha("two")).map(String::as_str), Some("alice"));
    let names = environment_principals(&e);
    assert!(names.contains("default") && names.contains("bob") && names.len() == 3);
}

// -- the resolver -----------------------------------------------------------------

#[test]
fn a_session_an_environment_token_issued_resolves_to_its_principal() {
    let (s, now) = (loaded(), wall_now());
    s.add(env_session(now));
    let hit = resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
        .unwrap()
        .unwrap();
    assert_eq!(
        (hit.principal.as_str(), hit.issued_by),
        ("default", PrincipalSource::Environment)
    );
    assert_eq!(hit.session.session_id, "s1");
    // The agent resolver never names it.
    assert!(matches!(
        crate::auth::resolve(
            Some(&format!("Bearer {SESSION_TOKEN}")),
            &env(),
            &loaded_store()
        ),
        crate::auth::Resolved::None
    ));
}

#[test]
fn a_session_ends_when_its_issuing_token_changes_or_its_principal_leaves() {
    let (s, now) = (loaded(), wall_now());
    s.add(env_session(now));
    let other = EnvTokens {
        single: Some("a-new-env-token".into()),
        map: vec![],
    };
    assert!(
        resolve_with(&s, SESSION_TOKEN, &other, &loaded_store(), now)
            .unwrap()
            .is_none()
    );
    // The same token now names another principal: gone too.
    let moved = EnvTokens {
        single: None,
        map: parse_token_map(&format!("{ENV_TOKEN}:alice")),
    };
    assert!(
        resolve_with(&s, SESSION_TOKEN, &moved, &loaded_store(), now)
            .unwrap()
            .is_none()
    );
}

#[test]
fn idle_and_absolute_lifetimes() {
    let (s, now) = (loaded(), wall_now());
    let day = 86400.0;
    // 31 days since creation, never used: idle.
    s.add(session(
        SESSION_TOKEN,
        "default",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "idle",
        now - 31.0 * day,
        None,
    ));
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
            .unwrap()
            .is_none()
    );
    // Created 60 days ago, used yesterday: alive past 30 days.
    s.add(session(
        "used",
        "default",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "used",
        now - 60.0 * day,
        Some(now - day),
    ));
    assert!(
        resolve_with(&s, "used", &env(), &loaded_store(), now)
            .unwrap()
            .is_some()
    );
    // Used yesterday but created 91 days ago: past the absolute limit,
    // even with its stored expiry pushed out.
    let mut old = session(
        "old",
        "default",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "old",
        now - 91.0 * day,
        Some(now - day),
    );
    old.expires_at = now + day;
    s.add(old);
    assert!(
        resolve_with(&s, "old", &env(), &loaded_store(), now)
            .unwrap()
            .is_none()
    );
}

#[test]
fn a_use_is_noted_and_extends_the_session() {
    let (s, now) = (loaded(), wall_now());
    let day = 86400.0;
    s.add(session(
        SESSION_TOKEN,
        "default",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "s1",
        now - 29.0 * day,
        None,
    ));
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
            .unwrap()
            .is_some()
    );
    let uses = s.take_uses();
    assert_eq!(uses.get("s1"), Some(&now));
    // 2 days on, the stored row alone would be idle; the noted use is not.
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now + 2.0 * day)
            .unwrap()
            .is_some()
    );
    // A failed write gives the uses back, never moving one backwards.
    s.give_back_uses(BTreeMap::from([("s1".to_string(), now)]));
    assert_eq!(s.take_uses().get("s1"), Some(&(now + 2.0 * day)));
}

#[test]
fn a_revoked_session_stops_at_once_and_stays_gone_through_an_older_refresh() {
    let (s, now) = (loaded(), wall_now());
    let row = env_session(now);
    s.add(row.clone());
    let read = s.begin_refresh(); // a refresh whose read began before the revocation
    s.drop_ids(&["s1".to_string()]);
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
            .unwrap()
            .is_none()
    );
    s.finish_refresh(read, vec![row.clone()], vec![]);
    assert!(
        s.lookup(&row.token_hash).is_none(),
        "an older read cannot undo it"
    );
    // A refresh that started after it is the database's word again.
    s.finish_refresh(s.begin_refresh(), vec![row.clone()], vec![]);
    assert!(s.lookup(&row.token_hash).is_some());
}

#[test]
fn a_sign_in_survives_a_refresh_that_began_before_it() {
    let (s, now) = (loaded(), wall_now());
    let read = s.begin_refresh();
    s.add(env_session(now));
    s.finish_refresh(read, vec![], vec![]);
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
            .unwrap()
            .is_some()
    );
    s.finish_refresh(s.begin_refresh(), vec![], vec![]);
    assert!(s.is_empty(), "a later read that does not hold it wins");
}

#[test]
fn a_stale_session_view_is_unavailable_not_unauthorized() {
    let now = wall_now();
    let s = SessionSnapshot::new();
    assert_eq!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now).err(),
        Some(Unavailable)
    );
    let long_ago = Instant::now()
        .checked_sub(STALE_AFTER + Duration::from_secs(1))
        .unwrap();
    s.finish_refresh(s.begin_refresh_at(long_ago), vec![env_session(now)], vec![]);
    assert_eq!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now).err(),
        Some(Unavailable)
    );
    // No bearer at all is not a question for the view.
    assert!(matches!(
        resolve(
            None,
            Some(&s),
            &HashMap::new(),
            &HashSet::new(),
            &loaded_store(),
            now
        ),
        Ok(None)
    ));
}

#[test]
fn a_session_a_paired_machine_issued_needs_its_row_and_a_fresh_store() {
    let (s, now) = (loaded(), wall_now());
    let store = loaded_store();
    s.add(session(
        STORE_SESSION_TOKEN,
        "laptop",
        SOURCE_STORE,
        &sha(STORED_TOKEN),
        "s2",
        now - 60.0,
        None,
    ));
    assert!(
        resolve_with(&s, STORE_SESSION_TOKEN, &env(), &store, now)
            .unwrap()
            .is_none()
    );
    stored(&store, "laptop", STORED_TOKEN);
    let hit = resolve_with(&s, STORE_SESSION_TOKEN, &env(), &store, now)
        .unwrap()
        .unwrap();
    assert_eq!(
        (hit.principal.as_str(), hit.issued_by),
        ("laptop", PrincipalSource::Store)
    );
    // Re-paired with a new token: the old token's session is gone.
    stored(&store, "laptop", "a-new-paired-token");
    assert!(
        resolve_with(&s, STORE_SESSION_TOKEN, &env(), &store, now)
            .unwrap()
            .is_none()
    );
    // A stale stored view: unavailable, never a refusal.
    let stale = PrincipalStore::new();
    let long_ago = Instant::now()
        .checked_sub(crate::auth::STALE_AFTER + Duration::from_secs(1))
        .unwrap();
    stale.refresh(vec![], None, &[], long_ago);
    stored(&stale, "laptop", STORED_TOKEN);
    assert_eq!(
        resolve_with(&s, STORE_SESSION_TOKEN, &env(), &stale, now).err(),
        Some(Unavailable)
    );
    // ... but an environment session does not depend on it.
    s.add(env_session(now));
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &stale, now)
            .unwrap()
            .is_some()
    );
}

#[test]
fn a_session_whose_stored_principal_is_revoked_stops() {
    let (s, now) = (loaded(), wall_now());
    let store = loaded_store();
    s.add(session(
        STORE_SESSION_TOKEN,
        "laptop",
        SOURCE_STORE,
        &sha(STORED_TOKEN),
        "s2",
        now - 60.0,
        None,
    ));
    store.add(StoredPrincipal {
        principal: "laptop".into(),
        token_hash: Some(sha(STORED_TOKEN)),
        tier: None,
        board: true,
        revoked: true,
        owned: true,
    });
    assert!(
        resolve_with(&s, STORE_SESSION_TOKEN, &env(), &store, now)
            .unwrap()
            .is_none()
    );
    assert!(!store.resolves("laptop"));
}

#[test]
fn a_passkey_session_needs_its_key_active() {
    let (s, now) = (loaded(), wall_now());
    let mut row = env_session(now);
    row.method = METHOD_PASSKEY.into();
    row.issuer = "credential-1".into();
    s.finish_refresh(s.begin_refresh(), vec![row.clone()], vec![]);
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
            .unwrap()
            .is_none()
    );
    s.finish_refresh(s.begin_refresh(), vec![row], vec!["credential-1".into()]);
    assert!(
        resolve_with(&s, SESSION_TOKEN, &env(), &loaded_store(), now)
            .unwrap()
            .is_some()
    );
}

#[test]
fn no_session_view_resolves_nothing() {
    assert!(
        resolve(
            Some(&format!("Bearer {SESSION_TOKEN}")),
            None,
            &HashMap::new(),
            &HashSet::new(),
            &loaded_store(),
            wall_now()
        )
        .unwrap()
        .is_none()
    );
}

#[test]
fn listing_adds_the_last_use_not_yet_written() {
    let (s, now) = (loaded(), wall_now());
    s.note_use("s1", now);
    let rows = vec![
        json!({"session_id": "s1", "last_used_at": now - 5.0}),
        json!({"session_id": "s2", "last_used_at": null}),
    ];
    let out = with_last_use(rows, &s);
    assert_eq!(out[0]["last_used_at"], json!(now));
    assert_eq!(out[1]["last_used_at"], Value::Null);
}

// -- the database ----------------------------------------------------------------

/// The test bank (`PL_W1A_TEST_DSN`) with its v59 tables, or `None` (and a
/// note) when there is none.
async fn bank() -> Option<(String, Client, tokio::task::JoinHandle<()>)> {
    let dsn = std::env::var("PL_W1A_TEST_DSN")
        .ok()
        .filter(|d| !d.is_empty());
    let Some(dsn) = dsn else {
        eprintln!("PL_W1A_TEST_DSN unset: skipping database test");
        return None;
    };
    let (c, t) = connect(&dsn).await.unwrap();
    let present: bool = c
        .query_one(
            "SELECT to_regclass('public.console_sessions') IS NOT NULL",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    if !present {
        eprintln!("console_sessions absent (bank below v59): skipping");
        return None;
    }
    Some((dsn, c, t))
}

async fn clean(c: &Client, tag: &str) {
    c.execute(
        "DELETE FROM console_sessions WHERE principal LIKE $1",
        &[&format!("{tag}%")],
    )
    .await
    .unwrap();
    c.execute(
        "DELETE FROM console_signin_codes WHERE principal LIKE $1",
        &[&format!("{tag}%")],
    )
    .await
    .unwrap();
}

fn yes(_: &str, _: &str, _: &str) -> Result<bool, Unavailable> {
    Ok(true)
}

#[tokio::test]
async fn db_a_code_stores_only_its_hash_redeems_once_and_only_at_its_origin() {
    let Some((dsn, c, _t)) = bank().await else {
        return;
    };
    clean(&c, "rs59a").await;
    let (code, expires) = issue_code(
        &dsn,
        "rs59a",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "http://localhost:8765",
    )
    .await
    .unwrap();
    assert!(expires > wall_now() + 200.0 && expires < wall_now() + 400.0);
    let stored: Vec<String> = c
        .query(
            "SELECT code_hash FROM console_signin_codes WHERE principal = 'rs59a'",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    assert_eq!(stored, vec![sha(&code)], "only the hash");
    // Another origin: refused, and the code is not spent.
    let other = redeem_code(
        &dsn,
        &sha(&code),
        &sha("b1"),
        "https://bank.example.com",
        "x",
        yes,
    )
    .await;
    assert_eq!(other, Ok(Redeemed::Refused));
    // An issuer gone: refused, not spent.
    let gone = redeem_code(
        &dsn,
        &sha(&code),
        &sha("b1"),
        "http://localhost:8765",
        "x",
        |_, _, _| Ok(false),
    )
    .await;
    assert_eq!(gone, Ok(Redeemed::Refused));
    // A stale stored view: unavailable, not spent.
    let stale = redeem_code(
        &dsn,
        &sha(&code),
        &sha("b1"),
        "http://localhost:8765",
        "x",
        |_, _, _| Err(Unavailable),
    )
    .await;
    assert_eq!(stale, Ok(Redeemed::Unavailable));
    let Ok(Redeemed::Session(row)) = redeem_code(
        &dsn,
        &sha(&code),
        &sha("b1"),
        "http://localhost:8765",
        "Firefox",
        yes,
    )
    .await
    else {
        panic!("the code redeems at its origin");
    };
    assert_eq!(
        (
            row.principal.as_str(),
            row.method.as_str(),
            row.label.as_str()
        ),
        ("rs59a", "link", "Firefox")
    );
    assert_eq!(
        (row.token_hash.clone(), row.issuer.clone()),
        (sha("b1"), sha(ENV_TOKEN))
    );
    assert!((row.expires_at - row.created_at - SESSION_MAX_S).abs() < 1.0);
    // clock_timestamp() is read once per column, as in Python's statement.
    assert!(
        row.last_used_at
            .is_some_and(|t| (t - row.created_at).abs() < 1.0)
    );
    // Spent.
    let again = redeem_code(
        &dsn,
        &sha(&code),
        &sha("b2"),
        "http://localhost:8765",
        "x",
        yes,
    )
    .await;
    assert_eq!(again, Ok(Redeemed::Refused));
    // A browser token hash signs in once.
    let (second, _) = issue_code(
        &dsn,
        "rs59a",
        SOURCE_ENVIRONMENT,
        &sha(ENV_TOKEN),
        "http://localhost:8765",
    )
    .await
    .unwrap();
    let dup = redeem_code(
        &dsn,
        &sha(&second),
        &sha("b1"),
        "http://localhost:8765",
        "x",
        yes,
    )
    .await;
    assert_eq!(dup, Ok(Redeemed::Refused));
    let unspent: Option<f64> = c
        .query_one(
            "SELECT used_at FROM console_signin_codes WHERE code_hash = $1",
            &[&sha(&second)],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(unspent, None, "the refusal rolled the spend back");
    // An expired code is refused.
    c.execute(
        "UPDATE console_signin_codes SET expires_at = 1 WHERE code_hash = $1",
        &[&sha(&second)],
    )
    .await
    .unwrap();
    let expired = redeem_code(
        &dsn,
        &sha(&second),
        &sha("b3"),
        "http://localhost:8765",
        "x",
        yes,
    )
    .await;
    assert_eq!(expired, Ok(Redeemed::Refused));
    clean(&c, "rs59a").await;
}

#[tokio::test]
async fn db_listing_revoking_loading_and_last_use() {
    let Some((dsn, c, _t)) = bank().await else {
        return;
    };
    clean(&c, "rs59b").await;
    let now = wall_now();
    let day = 86400.0;
    for (sid, principal, created, used) in [
        ("rs59b-1", "rs59b-a", now - 10.0, None),
        ("rs59b-2", "rs59b-b", now - 5.0, Some(now - 1.0)),
        (
            "rs59b-idle",
            "rs59b-a",
            now - 40.0 * day,
            Some(now - 31.0 * day),
        ),
    ] {
        c.execute(
            "INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, method, \
             label, created_at, last_used_at, expires_at) VALUES ($1, $2, $3, 'environment', $4, \
             'link', 'L', $5, $6, $7)",
            &[&sid, &sha(sid), &principal, &sha(ENV_TOKEN), &created, &used, &(created + SESSION_MAX_S)],
        )
        .await
        .unwrap();
    }
    let all = list_sessions(&dsn, None).await.unwrap();
    let ids: Vec<&str> = all
        .iter()
        .filter_map(|r| r["session_id"].as_str())
        .filter(|s| s.starts_with("rs59b"))
        .collect();
    assert_eq!(
        ids,
        ["rs59b-2", "rs59b-1"],
        "newest first; the idle one is not listed"
    );
    let keys: Vec<&String> = all[0].as_object().unwrap().keys().collect();
    assert_eq!(
        keys,
        [
            "session_id",
            "principal",
            "method",
            "label",
            "created_at",
            "last_used_at",
            "expires_at"
        ]
    );
    let mine = list_sessions(&dsn, Some("rs59b-a")).await.unwrap();
    assert_eq!(mine.len(), 1);
    // Revoking is limited to the caller's principal when it is given.
    assert!(
        !revoke_session(&dsn, "rs59b-2", Some("rs59b-a"))
            .await
            .unwrap()
    );
    assert!(revoke_session(&dsn, "rs59b-2", None).await.unwrap());
    assert!(
        !revoke_session(&dsn, "rs59b-2", None).await.unwrap(),
        "already revoked"
    );
    // The loader reads live sessions only.
    let (rows, _keys) = load_sessions(&c).await.unwrap();
    let live: Vec<&str> = rows
        .iter()
        .map(|r| r.session_id.as_str())
        .filter(|s| s.starts_with("rs59b"))
        .collect();
    assert!(live.contains(&"rs59b-1") && !live.contains(&"rs59b-2"));
    // A use never moves the last use backwards.
    let (mut writer, wt) = connect(&dsn).await.unwrap();
    flush_uses(
        &mut writer,
        &BTreeMap::from([("rs59b-1".to_string(), now + 5.0)]),
    )
    .await
    .unwrap();
    flush_uses(&mut writer, &BTreeMap::from([("rs59b-1".to_string(), now)]))
        .await
        .unwrap();
    close(writer, wt).await;
    let used: f64 = c
        .query_one(
            "SELECT last_used_at FROM console_sessions WHERE session_id = 'rs59b-1'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(used, now + 5.0);
    clean(&c, "rs59b").await;
}

async fn live_ids(c: &Client, principal: &str) -> Vec<String> {
    let mut ids: Vec<String> = c
        .query(
            "SELECT session_id FROM console_sessions WHERE principal = $1 AND revoked_at IS NULL",
            &[&principal],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    ids.sort();
    ids
}

#[tokio::test]
async fn db_revoking_or_re_pairing_a_principal_ends_its_sessions() {
    let Some((_dsn, mut c, _t)) = bank().await else {
        return;
    };
    clean(&c, "rs59c").await;
    let now = wall_now();
    for (sid, issuer, method) in [
        ("rs59c-old", sha("old-token"), "link"),
        ("rs59c-new", sha("new-token"), "link"),
        ("rs59c-key", "credential".to_string(), "passkey"),
    ] {
        c.execute(
            "INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, method, \
             label, created_at, last_used_at, expires_at) VALUES ($1, $2, 'rs59c', 'store', $3, $4, \
             '', $5, NULL, $6)",
            &[&sid, &sha(sid), &issuer, &method, &now, &(now + SESSION_MAX_S)],
        )
        .await
        .unwrap();
    }
    // A re-pair to new-token: link sessions other tokens issued end.
    let tx = c.transaction().await.unwrap();
    let n = revoke_principal_sessions(&tx, "rs59c", Some(METHOD_LINK), Some(&sha("new-token")))
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert_eq!(n, 1);
    assert_eq!(live_ids(&c, "rs59c").await, ["rs59c-key", "rs59c-new"]);
    // A revocation ends every one.
    let tx = c.transaction().await.unwrap();
    assert_eq!(
        revoke_principal_sessions(&tx, "rs59c", None, None)
            .await
            .unwrap(),
        2
    );
    tx.commit().await.unwrap();
    assert!(live_ids(&c, "rs59c").await.is_empty());
    clean(&c, "rs59c").await;
}

#[tokio::test]
async fn db_principal_revoke_and_redeem_revoke_sessions_in_their_transaction() {
    let Some((dsn, c, _t)) = bank().await else {
        return;
    };
    clean(&c, "rs59d").await;
    c.batch_execute("DELETE FROM principals WHERE principal = 'rs59d'")
        .await
        .unwrap();
    let now = wall_now();
    // A paired row with a pending code, as `invite --replace` leaves it.
    c.execute(
        "INSERT INTO principals (principal, token_hash, tier, board, created_at, code_hash, \
         code_expires_at) VALUES ('rs59d', $1, NULL, TRUE, 0, $2, $3)",
        &[&sha("rs59d-old"), &sha("RS59DCODE000"), &(now + 600.0)],
    )
    .await
    .unwrap();
    for (sid, issuer) in [
        ("rs59d-old", sha("rs59d-old")),
        ("rs59d-new", sha("rs59d-new")),
    ] {
        c.execute(
            "INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, method, \
             label, created_at, last_used_at, expires_at) VALUES ($1, $2, 'rs59d', 'store', $3, 'link', \
             '', $4, NULL, $5)",
            &[&sid, &sha(sid), &issuer, &now, &(now + SESSION_MAX_S)],
        )
        .await
        .unwrap();
    }
    let revoked = |sid: &'static str| {
        let c = &c;
        async move {
            c.query_one(
                "SELECT revoked_at IS NOT NULL FROM console_sessions WHERE session_id = $1",
                &[&sid],
            )
            .await
            .unwrap()
            .get::<_, bool>(0)
        }
    };
    let (mut w, wt) = connect(&dsn).await.unwrap();
    let paired = crate::principals::redeem(&mut w, &sha("RS59DCODE000"), &sha("rs59d-new"), &[])
        .await
        .unwrap();
    assert!(paired.is_some());
    assert!(
        revoked("rs59d-old").await,
        "the old token's session ends with the re-pair"
    );
    assert!(!revoked("rs59d-new").await, "the new token's is kept");
    assert!(crate::principals::revoke(&mut w, "rs59d").await.unwrap());
    assert!(
        revoked("rs59d-new").await,
        "a revocation ends every session"
    );
    close(w, wt).await;
    c.batch_execute("DELETE FROM principals WHERE principal = 'rs59d'")
        .await
        .unwrap();
    clean(&c, "rs59d").await;
}

#[tokio::test]
async fn db_old_codes_and_dead_sessions_are_pruned_after_a_day() {
    let Some((dsn, c, _t)) = bank().await else {
        return;
    };
    clean(&c, "rs59e").await;
    let now = wall_now();
    let day = 86400.0;
    c.execute(
        "INSERT INTO console_signin_codes (code_hash, principal, source, issuer, origin, expires_at, \
         created_at) VALUES ($1, 'rs59e', 'environment', 'i', 'o', $2, $2), ($3, 'rs59e', \
         'environment', 'i', 'o', $4, $4)",
        &[&sha("rs59e-old"), &(now - 2.0 * day), &sha("rs59e-recent"), &(now - 3600.0)],
    )
    .await
    .unwrap();
    c.execute(
        "INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, method, \
         label, created_at, last_used_at, expires_at, revoked_at) VALUES \
         ('rs59e-gone', $1, 'rs59e', 'environment', 'i', 'link', '', $3, NULL, $4, $5), \
         ('rs59e-kept', $2, 'rs59e', 'environment', 'i', 'link', '', $3, NULL, $4, $6)",
        &[
            &sha("rs59e-gone"),
            &sha("rs59e-kept"),
            &now,
            &(now + day),
            &(now - 2.0 * day),
            &(now - 3600.0),
        ],
    )
    .await
    .unwrap();
    issue_code(&dsn, "rs59e-issuer", SOURCE_ENVIRONMENT, "i", "o")
        .await
        .unwrap();
    let codes: i64 = c
        .query_one(
            "SELECT count(*) FROM console_signin_codes WHERE principal = 'rs59e'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(codes, 1, "only the recent code is kept");
    let kept: Vec<String> = c
        .query(
            "SELECT session_id FROM console_sessions WHERE principal = 'rs59e'",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    assert_eq!(kept, ["rs59e-kept"]);
    clean(&c, "rs59e").await;
}

/// The refresher loads the sessions on its connection after the
/// principals, and writes their last use back when due.
#[tokio::test]
async fn db_the_refresher_loads_sessions_and_writes_their_use() {
    let Some((dsn, c, _t)) = bank().await else {
        return;
    };
    clean(&c, "rs59f").await;
    let now = wall_now();
    c.execute(
        "INSERT INTO console_sessions (session_id, token_hash, principal, source, issuer, method, \
         label, created_at, last_used_at, expires_at) VALUES ('rs59f-1', $1, 'rs59f', 'environment', \
         'i', 'link', '', $2, NULL, $3)",
        &[&sha("rs59f-1"), &(now - 10.0), &(now + SESSION_MAX_S)],
    )
    .await
    .unwrap();
    let view = Arc::new(SessionSnapshot::new());
    let mut r =
        crate::principals::Refresher::new(dsn.clone(), Arc::new(PrincipalStore::new()), vec![])
            .with_sessions(Some(view.clone()));
    assert!(r.refresh_once().await);
    assert!(view.available());
    assert!(view.lookup(&sha("rs59f-1")).is_some());
    view.note_use("rs59f-1", now);
    assert!(r.flush_session_uses().await);
    let used: Option<f64> = c
        .query_one(
            "SELECT last_used_at FROM console_sessions WHERE session_id = 'rs59f-1'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(used, Some(now));
    view.note_use("rs59f-1", now + 1.0);
    assert!(!r.flush_session_uses().await, "within the minute: held");
    // A read-only (shadow) refresher loads them and never writes.
    let shadow_view = Arc::new(SessionSnapshot::new());
    let mut shadow =
        crate::principals::Refresher::new(dsn.clone(), Arc::new(PrincipalStore::new()), vec![])
            .with_sessions(Some(shadow_view.clone()))
            .with_read_only(true);
    assert!(shadow.refresh_once().await);
    assert!(shadow_view.lookup(&sha("rs59f-1")).is_some());
    shadow_view.note_use("rs59f-1", now + 50.0);
    assert!(!shadow.flush_session_uses().await);
    clean(&c, "rs59f").await;
}

use std::sync::Arc;
