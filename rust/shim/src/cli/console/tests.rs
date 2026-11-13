//! Pure pieces of `console` against values the oracle printed (CPython
//! 3.11.9, `console_cli.py` at this branch's base). Nothing here reads the
//! process environment or a home directory; the two transport tests use a
//! loopback listener of their own.
use super::*;

fn context() -> Context {
    Context {
        columns_80: true,
        colour_stdout: false,
        colour_stderr: false,
        utf8_argv: true,
    }
}

fn argv(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

/// A chooser for calls that must never ask which browser to start.
fn no_browser() -> Result<browser::Plan, browser::Unsupported> {
    panic!("this call chooses no browser")
}

fn read(text: &str) -> Dict {
    match pyjson::loads(text, DEPTH).unwrap() {
        J::Dict(dict) => dict,
        other => panic!("not an object: {other:?}"),
    }
}

#[test]
fn origins_normalize_as_the_oracle_does() {
    for (value, want) in [
        ("http://127.0.0.1:8765", Some("http://127.0.0.1:8765")),
        ("HTTP://Example.COM:80/", Some("http://example.com")),
        ("https://h:443", Some("https://h")),
        ("https://h:0443", Some("https://h")),
        ("http://h:", Some("http://h")),
        ("http://h:0", Some("http://h:0")),
        ("http://h?", Some("http://h")),
        ("http://h/?", Some("http://h")),
        ("http://[::1]:8765", Some("http://[::1]:8765")),
        ("http://h%41B:1", Some("http://h%41B:1")),
        ("https://H.example", Some("https://h.example")),
        ("http://h#", None),
        ("http://h/x", None),
        ("http://@h", None),
        ("http://u:p@h", None),
        ("http://h:99999", None),
        ("http://h:8a", None),
        ("ftp://h", None),
        ("127.0.0.1:8765", None),
        ("localhost:8765", None),
        ("http://[::1", None),
        ("http://[127.0.0.1]", None),
        ("http://", None),
        ("http:///", None),
        ("http://h:+1", None),
        ("http://h:-1", None),
        ("http://a b", None),
        ("http://h\u{7f}", None),
        ("http:h", None),
        ("//h:1", None),
        ("", None),
    ] {
        assert_eq!(
            normalize_origin(value).unwrap().as_deref(),
            want,
            "{value:?}"
        );
    }
    assert!(
        normalize_origin(&format!("http://{}", "a".repeat(250)))
            .unwrap()
            .is_none()
    );
    // Non-ASCII reaches urlsplit's NFKC checks: not reproduced.
    assert!(normalize_origin("http://h:\u{ff11}").is_err());
}

#[test]
fn route_ids_are_whole_lowercase_words() {
    for good in ["board", "a", "a-1", &"a".repeat(32)] {
        assert!(valid_route_id(good), "{good}");
    }
    for bad in ["", "Board", "1a", "-a", "a_b", "é", &"a".repeat(33), "a\n"] {
        assert!(!valid_route_id(bad), "{bad:?}");
    }
}

#[test]
fn argument_answers_match_the_oracle() {
    let usage = args::usage();
    let empty = || Out::stderr(EMPTY_REVOKE.into(), 2);
    let cases: [(&[&str], Out); 10] = [
        (&["--revoke", ""], empty()),
        (&["--no-open", "--revoke="], empty()),
        // Before the --list and --to checks.
        (&["--list", "--revoke="], empty()),
        (&["--revoke", "", "--to", "Bad"], empty()),
        (
            &["--to"],
            Out::stderr(
                format!(
                    "{usage}pseudolife-mcp console: error: argument --to: expected one argument\n"
                ),
                2,
            ),
        ),
        (
            &["--json=1"],
            Out::stderr(
                format!(
                    "{usage}pseudolife-mcp console: error: argument --json: ignored explicit argument '1'\n"
                ),
                2,
            ),
        ),
        (
            &["zz"],
            Out::stderr(
                format!("{usage}pseudolife-mcp console: error: unrecognized arguments: zz\n"),
                2,
            ),
        ),
        (
            &["--list", "--revoke", "x"],
            Out::stderr(
                "--list and --revoke are separate commands; give one\n".into(),
                2,
            ),
        ),
        (
            &["--no-open", "--to", "Bad"],
            Out::stderr(
                "--to takes a Console page id such as board, not 'Bad'\n".into(),
                2,
            ),
        ),
        (&["-h"], Out::stdout(args::HELP.into(), 0)),
    ];
    for (items, want) in cases {
        match plan(&argv(items), &context()) {
            Ok(Ok(out)) => assert_eq!(out, want, "{items:?}"),
            _ => panic!("{items:?} did not answer"),
        }
    }
}

#[test]
fn help_and_usage_need_the_reproduced_width_and_no_colour() {
    for (columns_80, colour) in [(false, false), (true, true)] {
        let context = Context {
            columns_80,
            colour_stdout: colour,
            colour_stderr: colour,
            utf8_argv: true,
        };
        assert!(plan(&argv(&["--help"]), &context).is_err());
        assert!(plan(&argv(&["--to"]), &context).is_err());
        // main's own checks print plainly.
        assert!(matches!(
            plan(&argv(&["--list", "--revoke", "x"]), &context),
            Ok(Ok(_))
        ));
    }
    let latin = Context {
        utf8_argv: false,
        ..context()
    };
    assert!(plan(&argv(&["--revoke", "é"]), &latin).is_err());
}

#[test]
fn every_sign_in_shape_makes_its_call() {
    let calls: [(&[&str], &str); 8] = [
        (&[], "link open"),
        (&["--to", "board"], "link board open"),
        (&["--json"], "link open json"),
        (&["--no-open"], "link"),
        (&["--no-open", "--to", "board", "--json"], "link board json"),
        (&["--no-open", "--unless-signed-in"], "link unless"),
        (&["--unless-signed-in"], "link open unless"),
        (&["--revoke", "s-1", "--to", "board"], "revoke s-1"),
    ];
    for (items, want) in calls {
        let (call, json) = match plan(&argv(items), &context()) {
            Ok(Err(call)) => call,
            _ => panic!("{items:?} made no call"),
        };
        let mut shown = match call {
            Call::List => "list".to_owned(),
            Call::Revoke(id) => format!("revoke {id}"),
            Call::Link { to, open, unless } => {
                let mut shown = "link".to_owned();
                if let Some(to) = to {
                    shown.push_str(&format!(" {to}"));
                }
                if open {
                    shown.push_str(" open");
                }
                if unless {
                    shown.push_str(" unless");
                }
                shown
            }
        };
        if json {
            shown.push_str(" json");
        }
        assert_eq!(shown, want, "{items:?}");
    }
    let (call, _) = match plan(&argv(&["--list", "--unless-signed-in"]), &context()) {
        Ok(Err(call)) => call,
        _ => panic!("no call"),
    };
    assert!(matches!(call, Call::List));
}

#[tokio::test(flavor = "current_thread")]
async fn a_launcher_this_port_lacks_defers_before_any_request() {
    // The choice comes first: no connection is attempted (were one, the
    // closed loopback port would answer `no_daemon`, not a deferral).
    let link = Call::Link {
        to: None,
        open: true,
        unless: false,
    };
    let unsupported = || Err(browser::Unsupported);
    assert!(
        answer(link, "http://127.0.0.1:9", "t", false, &unsupported)
            .await
            .is_err()
    );
}

#[test]
fn refusals_read_as_the_oracle_reads_them() {
    let text = |status, body: Option<&str>| {
        let payload = body.map(read);
        String::from_utf16(&refusal(status, payload.as_ref()).unwrap().py_str_units()).unwrap()
    };
    assert_eq!(
        text(403, Some(r#"{"error": "zz", "hint": ""}"#)),
        "the daemon answered HTTP 403 (zz)"
    );
    assert_eq!(
        text(403, Some(r#"{"error": "signin_unavailable"}"#)),
        "signin_unavailable"
    );
    assert_eq!(
        text(
            403,
            Some(r#"{"error": "signin_unavailable", "hint": "use https"}"#)
        ),
        "use https"
    );
    assert_eq!(
        text(500, Some(r#"{"error": 1.0}"#)),
        "the daemon answered HTTP 500 (1.0)"
    );
    assert_eq!(text(403, Some(r#"{"error": 5, "hint": 7}"#)), "7");
    assert!(
        text(405, None).starts_with("this daemon does not offer Console sign-in yet (HTTP 405)")
    );
    assert!(
        text(401, Some(r#"{"error": "unauthorized", "hint": "x"}"#))
            .starts_with("the daemon refused this machine's token")
    );
    assert_eq!(text(200, None), "the daemon answered HTTP 200");
    // An unhashable code reads as an unknown one.
    assert_eq!(
        text(403, Some(r#"{"error": ["x"]}"#)),
        "the daemon answered HTTP 403 (['x'])"
    );
    assert_eq!(
        text(403, Some(r#"{"error": {"a": 1}}"#)),
        "the daemon answered HTTP 403 ({'a': 1})"
    );
    assert_eq!(text(403, Some(r#"{"error": [], "hint": "h"}"#)), "h");
    assert_eq!(
        text(404, Some(r#"{"error": {}}"#)).get(..14),
        Some("this daemon do")
    );
    // The JSON detail keeps the hint's type.
    assert_eq!(
        refusal(403, Some(&read(r#"{"hint": 7}"#))).unwrap(),
        J::Int("7".into())
    );
}

#[test]
fn the_session_list_renders_as_the_oracle_prints_it() {
    let payload = read(
        r#"{"sessions": [{"session_id": "s-1", "principal": "alice", "method": "link", "label": "Firefox on box"},
        {"session_id": null, "principal": 1.5, "label": ""}, "skip", {"label": [1, "a"]}], "operator": false}"#,
    );
    let want = concat!(
        "ID                SIGNED IN AS         HOW      SINCE             LAST USED         BROWSER\n",
        "s-1               alice                link     -                 -                 Firefox on box\n",
        "None              1.5                           -                 -                 -\n",
        "                                                -                 -                 [1, 'a']\n",
        "(Only your own sign-ins are shown: this machine's token is a paired one.)\n",
    );
    assert_eq!(
        listed(&payload, false).unwrap(),
        Out::stdout(want.into(), 0)
    );
    assert_eq!(
        listed(&read(r#"{"sessions": {}}"#), false).unwrap().text,
        "No browsers are signed in. `pseudolife-mcp console` signs one in.\n"
    );
    assert_eq!(
        listed(
            &read(r#"{"sessions": [{"x": NaN}], "operator": true}"#),
            true
        )
        .unwrap()
        .text,
        "{\"state\": \"listed\", \"sessions\": [{\"x\": NaN}], \"operator\": true}\n"
    );
    // A time localtime refuses is the list's refusal, before any line.
    let unshowable = |field: &str| {
        Out::stderr(
            format!(
                "pseudolife-mcp console: the daemon's session list had a time this machine cannot show ({field})\n"
            ),
            4,
        )
    };
    for (body, field) in [
        (r#"{"sessions": [{"created_at": NaN}]}"#, "created_at"),
        (r#"{"sessions": [{"created_at": Infinity}]}"#, "created_at"),
        (r#"{"sessions": [{"created_at": -Infinity}]}"#, "created_at"),
        (r#"{"sessions": [{"last_used_at": 1e20}]}"#, "last_used_at"),
        (
            r#"{"sessions": [{"last_used_at": 9223372036854775808.0}]}"#,
            "last_used_at",
        ),
        (
            r#"{"sessions": [{"created_at": 123456789012345678901234567890}]}"#,
            "created_at",
        ),
        (
            r#"{"sessions": [{"created_at": -123456789012345678901234567890}]}"#,
            "created_at",
        ),
        (
            r#"{"sessions": [{"created_at": 1}, {"last_used_at": NaN, "created_at": 2}]}"#,
            "last_used_at",
        ),
        (
            r#"{"sessions": [{"created_at": NaN, "last_used_at": NaN}]}"#,
            "created_at",
        ),
    ] {
        assert_eq!(
            listed(&read(body), false).unwrap(),
            unshowable(field),
            "{body}"
        );
    }
    // A lone surrogate the list would print is its refusal too.
    for body in [
        r#"{"sessions": [{"label": "\udc80"}]}"#,
        r#"{"sessions": [{"session_id": "a"}, {"principal": "x\ud800"}]}"#,
    ] {
        assert_eq!(
            listed(&read(body), false).unwrap(),
            Out::stderr(format!("pseudolife-mcp console: {LIST_TEXT}\n"), 4),
            "{body}"
        );
    }
    // A time localtime converts past the formatted range defers.
    assert!(
        listed(
            &read(r#"{"sessions": [{"created_at": 32510000000}]}"#),
            false
        )
        .is_err()
    );
    // Windows' CRT refuses a negative time; glibc converts it.
    let negative = listed(&read(r#"{"sessions": [{"created_at": -5}]}"#), false).unwrap();
    if cfg!(windows) {
        assert_eq!(negative, unshowable("created_at"));
    } else {
        assert_eq!(negative.code, 0);
    }
    // Code points, not UTF-16 units, pad a cell.
    assert_eq!(
        String::from_utf16(&padded(units("\u{1f600}"), 3)).unwrap(),
        "\u{1f600}  "
    );
}

#[test]
fn a_printed_link_matches_the_oracle() {
    let link = |extra: &str| {
        read(&format!(
            r#"{{"url": "http://127.0.0.1:1/ui/#/signin?code=ABCD", "code": "ABCD", "origin": "http://127.0.0.1:1", "principal": "p"{extra}}}"#
        ))
    };
    let text = |minutes: u32| {
        format!(
            "Open this link to sign in to the Console:\n  http://127.0.0.1:1/ui/#/signin?code=ABCD\nOr type the code ABCD into the Console's sign-in dialog at http://127.0.0.1:1/ui/. It works once, within {minutes} minutes.\n"
        )
    };
    for (extra, minutes) in [
        ("", 5),
        (r#", "expires_in": 300"#, 5),
        (r#", "expires_in": "300""#, 5),
        (r#", "expires_in": " 300""#, 5),
        (r#", "expires_in": 59.9"#, 1),
        (r#", "expires_in": true"#, 1),
        (r#", "expires_in": 0"#, 5),
        (r#", "expires_in": -600"#, 1),
        (r#", "expires_in": 7200"#, 120),
    ] {
        assert_eq!(
            opened(&link(extra), false, None, None),
            Out::stdout(text(minutes), 0),
            "{extra}"
        );
    }
    assert_eq!(
        opened(&link(r#", "expires_in": 300"#), true, None, None).text,
        "{\"state\": \"printed\", \"url\": \"http://127.0.0.1:1/ui/#/signin?code=ABCD\", \"code\": \"ABCD\", \"origin\": \"http://127.0.0.1:1\", \"principal\": \"p\", \"expires_in\": 300}\n"
    );
    // An expiry int() refuses is the answer's refusal, in both modes.
    for extra in [
        r#", "expires_in": "abc""#,
        r#", "expires_in": [1]"#,
        r#", "expires_in": {"a": 1}"#,
        r#", "expires_in": NaN"#,
        r#", "expires_in": Infinity"#,
    ] {
        assert_eq!(
            opened(&link(extra), false, None, None),
            Out::stderr(format!("pseudolife-mcp console: {BAD_EXPIRY}\n"), 4),
            "{extra}"
        );
        assert_eq!(
            opened(&link(extra), true, None, None),
            Out::stdout(
                format!(
                    "{{\"state\": \"refused\", \"error\": \"bad_answer\", \"detail\": \"{BAD_EXPIRY}\"}}\n"
                ),
                4
            ),
            "{extra}"
        );
    }
    let refused = Out::stderr(format!("pseudolife-mcp console: {NO_LINK}\n"), 4);
    for body in [
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=A", "origin": "http://127.0.0.1:1"}"#,
        r#"{"url": "http://127.0.0.1:2/ui/#/signin?code=A", "code": "A", "origin": "http://127.0.0.1:1"}"#,
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=A B", "code": "A", "origin": "http://127.0.0.1:1"}"#,
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=A", "code": "A", "origin": "ftp://h"}"#,
        r#"{"url": 5, "code": "A", "origin": "http://127.0.0.1:1"}"#,
        // The oracle refuses these as well: no plain host, a surrogate.
        r#"{"url": "http://é/ui/#/signin?code=A", "code": "A", "origin": "http://é"}"#,
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=\udc80", "code": "A", "origin": "http://127.0.0.1:1"}"#,
        // Shell syntax after the code (a batch-file browser runs cmd.exe).
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=A&calc", "code": "A", "origin": "http://127.0.0.1:1"}"#,
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=A&to=board", "code": "A", "origin": "http://127.0.0.1:1"}"#,
    ] {
        assert_eq!(opened(&read(body), false, None, None), refused, "{body}");
    }
    assert_eq!(
        opened(&read(r#"{"code": 1}"#), true, None, None).text,
        "{\"state\": \"refused\", \"error\": \"bad_answer\", \"detail\": \"the daemon's answer had no sign-in link\"}\n"
    );
    // The page asked for may follow the code.
    let paged = read(
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=A&to=board", "code": "A", "origin": "http://127.0.0.1:1"}"#,
    );
    assert_eq!(opened(&paged, false, Some("board"), None).code, 0);
    assert_eq!(opened(&paged, false, Some("other"), None), refused);
}

#[test]
fn a_link_holds_nothing_but_the_origin_the_code_and_the_page_asked_for() {
    let origin = J::from("http://127.0.0.1:8765");
    let safe = |url: &str, to: Option<&str>| link_is_safe(Some(&J::from(url)), Some(&origin), to);
    let base = "http://127.0.0.1:8765/ui/#/signin?code=";
    for tail in [
        "ABCD&calc",
        "ABCD|calc",
        "ABCD^calc",
        "ABCD\"calc",
        "ABCD%PATH%",
        "ABCD>out",
        "ABCD<in",
        "ABCD(x)",
        "ABCD!x!",
        "abcd",
        "ABCI",
        "ABCL",
        "ABCO",
        "ABCU",
        "",
        "ABCD&to=Board",
        "ABCD&to=board&x",
        "ABCD&to=",
        "ABCD\u{660}",
        "ABCD#x",
        "ABCD?x",
        "ABCD/x",
    ] {
        assert!(!safe(&format!("{base}{tail}"), Some("board")), "{tail}");
    }
    for code in ["0123-4567-89AB", "CDEF-GHJK-MNPQ", "RSTV-WXYZ-0000"] {
        assert!(safe(&format!("{base}{code}"), None), "{code}");
        assert!(safe(&format!("{base}{code}"), Some("board")), "{code}");
        assert!(
            safe(&format!("{base}{code}&to=board"), Some("board")),
            "{code}"
        );
        assert!(!safe(&format!("{base}{code}&to=board"), None), "{code}");
    }
    // Hosts normalize_origin lets through but no plain link holds.
    for host in [
        "a&calc:8765",
        "a|b",
        "a^b",
        "a\"b",
        "a%25b",
        "[fe80::1%25eth0]:8765",
        "a!b",
        "a$b",
        "a'b",
        "a(b)",
        "a*b",
        "a,b",
        "a;b",
        "a=b",
        "a<b",
        "a>b",
        "a`b",
        "a{b}",
    ] {
        let raw = format!("http://{host}");
        // A zone id does not normalize here (the oracle's does, then
        // refuses its host): unsafe either way.
        let normal = normalize_origin(&raw)
            .ok()
            .flatten()
            .unwrap_or_else(|| raw.clone());
        let url = J::from(format!("{normal}/ui/#/signin?code=ABCD").as_str());
        assert!(
            !link_is_safe(Some(&url), Some(&J::from(raw.as_str())), None),
            "{raw}"
        );
    }
    for (raw, normal) in [
        ("http://localhost:8765", "http://localhost:8765"),
        ("http://[::1]:8765", "http://[::1]:8765"),
        ("https://console.example.com", "https://console.example.com"),
        (
            "HTTPS://Console.Example.COM:443/",
            "https://console.example.com",
        ),
        (
            "https://my_host.example:8443",
            "https://my_host.example:8443",
        ),
        (
            "http://[::ffff:127.0.0.1]:8765",
            "http://[::ffff:127.0.0.1]:8765",
        ),
    ] {
        let url = J::from(format!("{normal}/ui/#/signin?code=ABCD").as_str());
        assert!(link_is_safe(Some(&url), Some(&J::from(raw)), None), "{raw}");
    }
}

#[test]
fn the_browser_starts_only_for_a_safe_link_and_before_anything_prints() {
    let link = |extra: &str| {
        read(&format!(
            r#"{{"url": "http://127.0.0.1:1/ui/#/signin?code=AB", "code": "AB", "origin": "http://127.0.0.1:1"{extra}}}"#
        ))
    };
    let started = std::cell::RefCell::new(Vec::new());
    let start = |answer: bool| {
        let started = &started;
        move |url: &str| {
            started.borrow_mut().push(url.to_owned());
            answer
        }
    };
    let out = opened(&link(""), false, None, Some(&mut start(true)));
    assert_eq!(
        out,
        Out::stdout(
            "Opened the Console in your browser: confirm the sign-in there.\n  http://127.0.0.1:1/ui/#/signin?code=AB\nOr type the code AB into the Console's sign-in dialog at http://127.0.0.1:1/ui/. It works once, within 5 minutes.\n".into(),
            0
        )
    );
    let out = opened(&link(""), true, None, Some(&mut start(false)));
    assert!(
        out.text.starts_with("{\"state\": \"printed\", "),
        "{}",
        out.text
    );
    let out = opened(&link(""), true, None, Some(&mut start(true)));
    assert!(
        out.text.starts_with("{\"state\": \"opened\", "),
        "{}",
        out.text
    );
    // An unusable expiry, a code other than the link's and an unsafe link
    // start nothing.
    for body in [
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=AB", "code": "AB", "origin": "http://127.0.0.1:1", "expires_in": "x"}"#,
        r#"{"url": "http://127.0.0.1:1/ui/#/signin?code=AB", "code": "AC", "origin": "http://127.0.0.1:1"}"#,
        r#"{"url": "file:///etc/passwd", "code": "AB", "origin": "http://127.0.0.1:1"}"#,
    ] {
        let out = opened(&read(body), false, None, Some(&mut start(true)));
        assert_eq!(out.code, 4, "{body}");
    }
    assert_eq!(
        started.into_inner(),
        vec!["http://127.0.0.1:1/ui/#/signin?code=AB"; 3],
        "one start per usable answer"
    );
}

#[test]
fn a_code_other_than_the_links_is_refused_in_both_modes() {
    let body = |code: &str| {
        read(&format!(
            r#"{{"url": "http://127.0.0.1:1/ui/#/signin?code=A", "code": "{code}", "origin": "http://127.0.0.1:1"}}"#
        ))
    };
    for code in [
        r"x\udc80y",
        r"😀\ud800\udbff",
        r"\udc80",
        "B",
        "näme ✓",
        r"\u001b[2J",
    ] {
        assert_eq!(
            opened(&body(code), false, None, None),
            Out::stderr(format!("pseudolife-mcp console: {MISMATCH}\n"), 4),
            "{code}"
        );
        assert_eq!(opened(&body(code), true, None, None).code, 4, "{code}");
    }
}

#[test]
fn connect_failures_read_as_cpython_prints_them() {
    let code = if cfg!(windows) { 10061 } else { 111 };
    let text = reason(Some(&io::Error::from_raw_os_error(code))).unwrap();
    if cfg!(windows) {
        assert!(text.starts_with("[WinError 10061] "), "{text}");
        assert!(!text.ends_with('.'), "{text}");
    } else {
        assert_eq!(text, "[Errno 111] Connection refused");
    }
    // Only a refused connect is compared; a timeout or any other error defers.
    assert!(reason(None).is_err());
    let other = if cfg!(windows) { 10060 } else { 110 };
    assert!(reason(Some(&io::Error::from_raw_os_error(other))).is_err());
    assert!(reason(Some(&io::Error::other("no code"))).is_err());
}

/// Accepts one connection, returns its request bytes after answering `reply`.
async fn serve_once(reply: &'static [u8]) -> (String, tokio::task::JoinHandle<Vec<u8>>) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        let mut buffer = [0u8; 4096];
        loop {
            let Ok(read) = stream.read(&mut buffer).await else {
                break;
            };
            request.extend_from_slice(&buffer[..read]);
            let Some(end) = request.windows(4).position(|w| w == b"\r\n\r\n") else {
                if read == 0 {
                    break;
                }
                continue;
            };
            let head = String::from_utf8_lossy(&request[..end]).to_ascii_lowercase();
            let length = head
                .lines()
                .find_map(|line| line.strip_prefix("content-length: "))
                .map_or(0, |value| value.trim().parse::<usize>().unwrap());
            if request.len() >= end + 4 + length || read == 0 {
                break;
            }
        }
        // The client may close first; the request is what the test reads.
        let _ = stream.write_all(reply).await;
        let _ = stream.shutdown().await;
        request
    });
    (origin, task)
}

#[tokio::test(flavor = "current_thread")]
async fn a_list_request_carries_the_oracle_headers() {
    let (origin, task) = serve_once(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\nConnection: close\r\n\r\n{\"sessions\": []}",
    )
    .await;
    let out = answer(Call::List, &origin, "fixture-token", true, &no_browser)
        .await
        .unwrap();
    let request = String::from_utf8(task.await.unwrap()).unwrap();
    assert!(
        request.starts_with("GET /api/console/sessions HTTP/1.1\r\n"),
        "{request}"
    );
    let head = request.to_ascii_lowercase();
    // urllib sends the headers dict's Content-Type on a GET too.
    for field in [
        "\r\nauthorization: bearer fixture-token\r\n",
        "\r\ncontent-type: application/json\r\n",
        "\r\nuser-agent: python-urllib/3.11\r\n",
        "\r\naccept-encoding: identity\r\n",
        "\r\nconnection: close\r\n",
    ] {
        assert!(head.contains(field), "{field:?} missing from {request}");
    }
    assert!(!head.contains("content-length"), "{request}");
    assert_eq!(
        out.text,
        "{\"state\": \"listed\", \"sessions\": [], \"operator\": null}\n"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn a_link_request_posts_the_oracle_body_and_reads_a_refusal() {
    let (origin, task) = serve_once(
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 42\r\nConnection: close\r\n\r\n{\"error\": \"signin_unavailable\", \"hint\": 5}",
    )
    .await;
    let link = Call::Link {
        to: Some("board".into()),
        open: false,
        unless: false,
    };
    let out = answer(link, &origin, "t", true, &no_browser).await.unwrap();
    let request = String::from_utf8(task.await.unwrap()).unwrap();
    assert!(
        request.starts_with("POST /api/console/link HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.ends_with(&format!(
            "\r\n\r\n{{\"origin\": \"{origin}\", \"to\": \"board\"}}"
        )),
        "{request}"
    );
    assert_eq!(
        out,
        Out::stdout(
            "{\"state\": \"refused\", \"status\": 403, \"error\": \"signin_unavailable\", \"detail\": 5}\n".into(),
            4
        )
    );
}
